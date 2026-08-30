"""One generic persistence provider for every SQLModel table model."""
from __future__ import annotations

import importlib
import pkgutil
from typing import TypeVar

from injector import inject
from sqlmodel import SQLModel, Session, create_engine, select

from ai_pod_cli.config_store import ConfigStore

T = TypeVar("T", bound=SQLModel)


class ModelRepository:
    @inject
    def __init__(self, config_store: ConfigStore):
        url = config_store.get("database.url", "sqlite:///database.db")
        connect_args = {"check_same_thread": False} if str(url).startswith("sqlite") else {}
        self.engine = create_engine(url, connect_args=connect_args)

    def load_models(self, package_name: str = "modules.models") -> None:
        package = importlib.import_module(package_name)
        for item in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
            importlib.import_module(item.name)

    def init_db(self) -> None:
        self.load_models()
        SQLModel.metadata.create_all(self.engine)

    def save(self, instance: T) -> T:
        self.init_db()
        with Session(self.engine) as session:
            session.add(instance)
            session.commit()
            session.refresh(instance)
            return instance

    def get(self, model: type[T], object_id) -> T | None:
        self.init_db()
        with Session(self.engine) as session:
            return session.get(model, object_id)

    def list(self, model: type[T]) -> list[T]:
        self.init_db()
        with Session(self.engine) as session:
            return list(session.exec(select(model)).all())

    def find(self, model: type[T], filters: dict | None = None, **filter_values) -> list[T]:
        """Find rows using either ``find(Model, field=value)`` or a filter dict."""
        self.init_db()
        statement = select(model)
        criteria = dict(filters or {})
        criteria.update(filter_values)
        for name, value in criteria.items():
            statement = statement.where(getattr(model, name) == value)
        with Session(self.engine) as session:
            return list(session.exec(statement).all())

    def delete(self, instance: T) -> None:
        with Session(self.engine) as session:
            session.delete(instance)
            session.commit()

    def close(self) -> None:
        """Release pooled database connections, primarily for tests and shutdown."""
        self.engine.dispose()
