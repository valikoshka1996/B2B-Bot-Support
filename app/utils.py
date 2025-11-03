from .db import engine, SessionLocal
from .models import Base, Admin, Company, Client, Message, Claim
from sqlalchemy.orm import Session

def init_db(initial_admin_tg_id: str = None):
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        if initial_admin_tg_id:
            exists = session.query(Admin).filter_by(tg_id=str(initial_admin_tg_id)).first()
            if not exists:
                admin = Admin(tg_id=str(initial_admin_tg_id), name="Initial Admin", is_super=True)
                session.add(admin)
                session.commit()
            else:
                session.rollback()
    finally:
        session.close()

# === ADMIN CRUD ===
def get_admins(session: Session):
    return session.query(Admin).all()

def add_admin(session: Session, tg_id: str, name: str=None):
    a = Admin(tg_id=str(tg_id), name=name or "", is_super=False)
    session.add(a)
    session.commit()
    return a

def update_admin(session: Session, tg_id: str, new_name: str = None, is_super: bool = None):
    a = session.query(Admin).filter_by(tg_id=str(tg_id)).first()
    if not a:
        return None
    if new_name is not None:
        a.name = new_name
    if is_super is not None:
        a.is_super = is_super
    session.commit()
    return a

def delete_admin(session: Session, tg_id: str):
    a = session.query(Admin).filter_by(tg_id=str(tg_id)).first()
    if not a:
        return False
    session.delete(a)
    session.commit()
    return True

# === COMPANY CRUD ===
def add_company(session: Session, name, contact_name=None, client_id=None, client_secret=None):
    c = Company(name=name, contact_name=contact_name, client_id=client_id, client_secret=client_secret)
    session.add(c)
    session.commit()
    return c

def update_company(session: Session, company_id: int, name=None, contact_name=None, client_id=None, client_secret=None):
    c = session.query(Company).filter_by(id=company_id).first()
    if not c:
        return None
    if name is not None:
        c.name = name
    if contact_name is not None:
        c.contact_name = contact_name
    if client_id is not None:
        c.client_id = client_id
    if client_secret is not None:
        c.client_secret = client_secret
    session.commit()
    return c

def delete_company(session: Session, company_id: int):
    c = session.query(Company).filter_by(id=company_id).first()
    if not c:
        return False
    session.delete(c)
    session.commit()
    return True

# === CLIENT CRUD ===
def add_client(session: Session, tg_id: str, name: str=None, company_id: int=None):
    c = session.query(Client).filter_by(tg_id=str(tg_id)).first()
    if c:
        c.name = name or c.name
        c.company_id = company_id or c.company_id
    else:
        c = Client(tg_id=str(tg_id), name=name, company_id=company_id)
        session.add(c)
    session.commit()
    return c

def update_client(session: Session, tg_id: str, name=None, company_id=None):
    c = session.query(Client).filter_by(tg_id=str(tg_id)).first()
    if not c:
        return None
    if name is not None:
        c.name = name
    if company_id is not None:
        c.company_id = company_id
    session.commit()
    return c

def delete_client(session: Session, tg_id: str):
    c = session.query(Client).filter_by(tg_id=str(tg_id)).first()
    if not c:
        return False
    session.delete(c)
    session.commit()
    return True
 
def get_company_history(session: Session, company_id: int):
    """
    Повертає всі повідомлення по компанії (за client.company_id),
    відсортовані за часом.
    """
    from .models import Message, Client

    q = (
        session.query(Message)
        .join(Client, Client.tg_id == Message.client_tg_id)
        .filter(Client.company_id == company_id)
        .order_by(Message.created_at.asc())
    )
    return q.all()

