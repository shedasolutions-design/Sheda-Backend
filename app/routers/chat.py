from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status, Query, Path
from app.models.chat import ChatMessage
from app.models.user import BaseUser
from app.schemas.chat import ChatMessageSchema, SendMessageRequest, ConversationSchema, UnreadCountSchema
from app.schemas.user_schema import UserShow
from typing import Dict
from core.dependecies import DBSession
from app.services.user_service import ActiveUser, ActiveVerifiedWSUser
from typing import List
from sqlalchemy.future import select
from sqlalchemy.engine import Result
from sqlalchemy import or_, and_, func, case, desc
from core.logger import logger
from core.configs import settings
import json

router = APIRouter(
    prefix="/chat",
    tags=["Chat"],
)


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[Dict[int, WebSocket]] = []

    async def connect(self, websocket: WebSocket, user_id: int):
        await websocket.accept()
        self.active_connections.append({"user_id": user_id, "websocket": websocket}) # type: ignore

    def disconnect(self, websocket: WebSocket):
        self.active_connections = [
            conn for conn in self.active_connections if conn["websocket"] != websocket # type: ignore
        ]

    async def send_personal_message(self, message: dict, user_id: int):
        """Send a message to a specific user by ID"""
        for conn in self.active_connections:
            if conn["user_id"] == user_id:
                await conn["websocket"].send_json(message)
                break  # stop after sending to first matching user

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            await connection["websocket"].send_text(message) # type: ignore
    


manager = ConnectionManager()



@router.websocket("/ws")
async def websocket_chat(
    websocket: WebSocket,
    current_user: ActiveVerifiedWSUser,
    db: DBSession,
):
    if not current_user:
        logger.info("User not found")
        return  # user was invalid, websocket already closed in dependency

    await manager.connect(websocket, current_user.id)
    current_user = UserShow.model_validate(current_user)

    try:
        while True:
            try:
                data = await websocket.receive_json()
            except Exception:
                await websocket.send_text("Invalid JSON received")
                continue

            sender_id = current_user.id
            receiver_id = data.get("receiver_id")
            message = data.get("message")

            if not all([sender_id, receiver_id, message]):
                await websocket.send_text("Missing fields in message")
                continue

            # Store in DB
            chat_message = ChatMessageSchema(
                sender_id=sender_id, receiver_id=receiver_id, message=message
            )
            db_message = ChatMessage(**chat_message.model_dump(exclude_unset=True))
            db.add(db_message)
            await db.commit()
            await db.refresh(db_message)
            
        
            
            sender_info = {
                "id": sender_id,
                "username": current_user.username,
                "avatar_url": current_user.profile_pic,
                }
            payload = {
                "id": db_message.id,
                "sender_info": sender_info,
                "receiver_id": db_message.receiver_id,
                "message": db_message.message,
                "created_at": db_message.timestamp.isoformat() if db_message.timestamp else None,
                }


            await manager.send_personal_message(payload, receiver_id)


    except WebSocketDisconnect:
        manager.disconnect(websocket)


@router.get(
    "/chat-history",
    response_model=List[ChatMessageSchema],
    status_code=status.HTTP_200_OK,
)
async def chat_history(current_user: ActiveUser, db: DBSession,
                       offset: int = Query(0, ge=0, description="Number of messages to skip"),
    limit: int = Query(100, ge=1, le=200, description="Number of messages to return"),):
    query = select(ChatMessage).where(ChatMessage.sender_id == current_user.id).order_by(ChatMessage.timestamp.desc()).offset(offset).limit(limit)
    result: Result = await db.execute(query)
    chats = result.scalars().all()
    return chats


@router.get(
    "/conversations",
    response_model=List[ConversationSchema],
    status_code=status.HTTP_200_OK,
)
async def get_conversations(current_user: ActiveUser, db: DBSession):
    """Get a list of all active conversations for the current user."""
    user_id = current_user.id

    # Subquery to get the latest message for each conversation
    latest_msg_subquery = (
        select(
            case(
                (ChatMessage.sender_id == user_id, ChatMessage.receiver_id),
                else_=ChatMessage.sender_id
            ).label("other_user_id"),
            func.max(ChatMessage.id).label("max_id")
        )
        .where(or_(ChatMessage.sender_id == user_id, ChatMessage.receiver_id == user_id))
        .group_by(
            case(
                (ChatMessage.sender_id == user_id, ChatMessage.receiver_id),
                else_=ChatMessage.sender_id
            )
        )
    ).subquery()

    # Get latest messages with user info
    query = (
        select(
            ChatMessage,
            BaseUser.username.label("other_user_name"),
        )
        .join(latest_msg_subquery, ChatMessage.id == latest_msg_subquery.c.max_id)
        .join(
            BaseUser,
            BaseUser.id == latest_msg_subquery.c.other_user_id
        )
        .order_by(desc(ChatMessage.timestamp))
    )

    result = await db.execute(query)
    conversations_data = result.all()

    # Get unread counts for each conversation
    conversations = []
    for row in conversations_data:
        chat_msg = row[0]
        other_user_name = row.other_user_name
        other_user_id = chat_msg.sender_id if chat_msg.sender_id != user_id else chat_msg.receiver_id

        # Count unread messages from this user
        unread_query = (
            select(func.count(ChatMessage.id))
            .where(
                and_(
                    ChatMessage.sender_id == other_user_id,
                    ChatMessage.receiver_id == user_id,
                    ChatMessage.is_read == False
                )
            )
        )
        unread_result = await db.execute(unread_query)
        unread_count = unread_result.scalar() or 0

        conversations.append(
            ConversationSchema(
                other_user_id=other_user_id,
                other_user_name=other_user_name,
                last_message=chat_msg.message,
                timestamp=chat_msg.timestamp,
                unread_count=unread_count
            )
        )

    return conversations


@router.get(
    "/history/{user_id}",
    response_model=List[ChatMessageSchema],
    status_code=status.HTTP_200_OK,
)
async def get_history_with_user(
    current_user: ActiveUser,
    db: DBSession,
    user_id: int = Path(..., description="The ID of the other user"),
    offset: int = Query(0, ge=0, description="Number of messages to skip"),
    limit: int = Query(100, ge=1, le=200, description="Number of messages to return"),
):
    """Get the message history between the current user and another user."""
    query = (
        select(ChatMessage)
        .where(
            or_(
                and_(ChatMessage.sender_id == current_user.id, ChatMessage.receiver_id == user_id),
                and_(ChatMessage.sender_id == user_id, ChatMessage.receiver_id == current_user.id)
            )
        )
        .order_by(ChatMessage.timestamp.desc())
        .offset(offset)
        .limit(limit)
    )
    result: Result = await db.execute(query)
    chats = result.scalars().all()
    return chats


@router.post(
    "/send-message",
    response_model=ChatMessageSchema,
    status_code=status.HTTP_201_CREATED,
)
async def send_message(
    current_user: ActiveUser,
    db: DBSession,
    request: SendMessageRequest,
):
    """Send a message to another user."""
    chat_message = ChatMessage(
        sender_id=current_user.id,
        receiver_id=request.receiver_id,
        message=request.message,
        property_id=request.property_id,
    )
    db.add(chat_message)
    await db.commit()
    await db.refresh(chat_message)

    # Also send via WebSocket if recipient is connected
    sender_info = {
        "id": current_user.id,
        "username": current_user.username,
        "avatar_url": current_user.profile_pic,
    }
    payload = {
        "id": chat_message.id,
        "sender_info": sender_info,
        "receiver_id": chat_message.receiver_id,
        "message": chat_message.message,
        "created_at": chat_message.timestamp.isoformat() if chat_message.timestamp else None,
    }
    await manager.send_personal_message(payload, request.receiver_id)

    return chat_message


@router.post(
    "/mark-read/{sender_id}",
    status_code=status.HTTP_200_OK,
)
async def mark_messages_as_read(
    current_user: ActiveUser,
    db: DBSession,
    sender_id: int = Path(..., description="The ID of the sender whose messages to mark as read"),
):
    """Mark all messages from a specific user as read."""
    query = (
        select(ChatMessage)
        .where(
            and_(
                ChatMessage.sender_id == sender_id,
                ChatMessage.receiver_id == current_user.id,
                ChatMessage.is_read == False
            )
        )
    )
    result = await db.execute(query)
    messages = result.scalars().all()

    for message in messages:
        message.is_read = True
        db.add(message)

    await db.commit()

    return {"message": f"Marked {len(messages)} messages as read"}


@router.get(
    "/unread-count",
    response_model=UnreadCountSchema,
    status_code=status.HTTP_200_OK,
)
async def get_unread_count(current_user: ActiveUser, db: DBSession):
    """Get the total number of unread messages for the notification badge."""
    query = (
        select(func.count(ChatMessage.id))
        .where(
            and_(
                ChatMessage.receiver_id == current_user.id,
                ChatMessage.is_read == False
            )
        )
    )
    result = await db.execute(query)
    unread_count = result.scalar() or 0

    return UnreadCountSchema(unread_count=unread_count)
