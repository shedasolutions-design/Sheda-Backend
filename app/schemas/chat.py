from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List


class ChatMessageSchema(BaseModel):
    sender_id: int
    receiver_id: int
    message: str
    timestamp: Optional[datetime] = None
    property_id: Optional[int] = None

    class Config:
        from_attributes = True


class SendMessageRequest(BaseModel):
    receiver_id: int
    message: str
    property_id: Optional[int] = None


class ConversationSchema(BaseModel):
    other_user_id: int
    other_user_name: Optional[str] = None
    last_message: str
    timestamp: datetime
    unread_count: int

    class Config:
        from_attributes = True


class UnreadCountSchema(BaseModel):
    unread_count: int
