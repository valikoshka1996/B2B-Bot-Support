from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, func
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime

Base = declarative_base()


class Admin(Base):
    __tablename__ = 'admins'

    id = Column(Integer, primary_key=True)
    tg_id = Column(String(32), unique=True)  # ✅ вказали довжину
    name = Column(String(100))
    is_super = Column(Integer, default=0)

    messages = relationship("Message", back_populates="admin")


class Company(Base):
    __tablename__ = 'companies'

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    contact_name = Column(String(100))
    client_id = Column(String(255))
    client_secret = Column(String(255))

    clients = relationship("Client", back_populates="company")


class Client(Base):
    __tablename__ = 'clients'

    id = Column(Integer, primary_key=True)
    tg_id = Column(String(32), unique=True)
    name = Column(String(100))
    company_id = Column(Integer, ForeignKey('companies.id'))

    company = relationship("Company", back_populates="clients")
    messages = relationship("Message", back_populates="client")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    client_tg_id = Column(String(32), ForeignKey("clients.tg_id"))
    admin_tg_id = Column(String(32), ForeignKey("admins.tg_id"), nullable=True)
    direction = Column(String(20))
    text = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    company_snapshot = Column(String(255), nullable=True)
    file_id = Column(String(255), nullable=True)
    file_type = Column(String(50), nullable=True)
    file_path = Column(String(255), nullable=True)

    client = relationship("Client", back_populates="messages")
    admin = relationship("Admin", back_populates="messages")


class Claim(Base):
    __tablename__ = 'claims'

    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey('clients.id'))
    admin_id = Column(Integer, ForeignKey('admins.id'), nullable=True)
    title = Column(String(255), nullable=False)
    message_id = Column(Integer, ForeignKey('messages.id'))
    description = Column(Text)
    status = Column(String(20), default='open')  # open / in_progress / closed
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())

    client = relationship("Client")
    admin = relationship("Admin")
