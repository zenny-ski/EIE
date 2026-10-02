"""Wires the components together. The CLI (and later the GUI/API) builds everything from here."""
from dataclasses import dataclass
from typing import Optional

from .classification import build_classifier
from .config import Settings
from .ingestion import build_source
from .pipeline import Pipeline
from .adminconfig import AdminConfig
from .notify import Notifier
from .reply import Mailer, ReplyService
from .subscription import SubscriptionManager
from .review import ReviewService
from .routing import Dispatcher
from .storage import Store


@dataclass
class App:
    settings: Settings
    store: Store
    dispatcher: Dispatcher
    reviews: ReviewService
    replies: ReplyService
    notifier: Notifier
    admin: AdminConfig
    subscriptions: SubscriptionManager

    def pipeline(self, source=None) -> Pipeline:
        """Builds a pipeline; `source` overrides the configured mailbox (e.g. a JsonFileSource)."""
        store = self.store

        def source_fallback(exc: Exception) -> None:
            store.audit("source.fallback", from_source="graph", to_source="imap", error=repr(exc))

        def classifier_failure(email, exc: Exception) -> None:
            store.audit("classifier.error", message_id=email.message_id, error=repr(exc))

        return Pipeline(
            self.settings,
            store,
            source or build_source(self.settings, source_fallback),
            build_classifier(self.settings, classifier_failure),
            self.dispatcher,
            self.notifier,
        )


def create_app(settings: Optional[Settings] = None) -> App:
    settings = settings or Settings.from_env()
    store = Store(settings.db_path, settings.timezone)
    dispatcher = Dispatcher(settings)
    mailer = Mailer(settings)
    reviews = ReviewService(store, dispatcher, settings, mailer)
    notifier = Notifier(settings, mailer, store)
    return App(settings, store, dispatcher, reviews, ReplyService(store, mailer), notifier,
               AdminConfig(store, settings), SubscriptionManager(settings, store, notifier))
