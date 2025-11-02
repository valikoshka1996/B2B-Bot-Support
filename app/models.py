from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, func
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime

Base = declarative_base()


class Admin(Base):
    __tablename__ = 'admins'

    id = Column(Integer, primary_key=True)
    tg_id = Column(String, unique=True)
    name = Column(String)
    is_super = Column(Integer, default=0)

    messages = relationship("Message", back_populates="admin")


class Company(Base):
    __tablename__ = 'companies'

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    contact_name = Column(String)
    client_id = Column(String)
    client_secret = Column(String)

    clients = relationship("Client", back_populates="company")


class Client(Base):
    __tablename__ = 'clients'

    id = Column(Integer, primary_key=True)
    tg_id = Column(String, unique=True)
    name = Column(String)
    company_id = Column(Integer, ForeignKey('companies.id'))

    company = relationship("Company", back_populates="clients")
    messages = relationship("Message", back_populates="client")


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    client_tg_id = Column(String, ForeignKey("clients.tg_id"))
    admin_tg_id = Column(String, ForeignKey("admins.tg_id"), nullable=True)
    direction = Column(String)
    text = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    company_snapshot = Column(String, nullable=True)
    file_id = Column(String, nullable=True)  # ✅ додаємо
    file_type = Column(String, nullable=True)  # ✅ тип файлу (photo/document/video/voice)
    file_path = Column(String, nullable=True)
    client = relationship("Client", back_populates="messages")
    admin = relationship("Admin", back_populates="messages")

class Claim(Base):
    __tablename__ = 'claims'

    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey('clients.id'))
    admin_id = Column(Integer, ForeignKey('admins.id'), nullable=True)
    title = Column(String, nullable=False)
    message_id = Column(Integer, ForeignKey('messages.id'))  # <- Додаємо сюди
    description = Column(Text)
    status = Column(String, default='open')  # open / in_progress / closed
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())

    client = relationship("Client")
    admin = relationship("Admin")
