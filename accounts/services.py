"""
Dynamic system-reset service.

Automatically discovers every model in every *business* app, orders
them so foreign-key children are deleted before parents, and wipes
all records — except the super-admin account.

Adding a new business app/model requires **zero** changes here;
the discovery is driven by ``INSTALLED_APPS`` at runtime.
"""

from django.apps import apps as django_apps
from django.db import models as db_models, transaction

# ── Framework / infrastructure app labels that must NEVER be wiped ──
# These map to the *label* Django assigns (the last segment of the
# dotted app name).  Add future third-party utility apps here.
SYSTEM_APP_LABELS = frozenset(
    {
        "admin",                  # django.contrib.admin
        "auth",                   # django.contrib.auth
        "contenttypes",           # django.contrib.contenttypes
        "sessions",               # django.contrib.sessions
        "messages",               # django.contrib.messages
        "staticfiles",            # django.contrib.staticfiles
        "rest_framework",         # DRF
        "rest_framework_simplejwt",
        "token_blacklist",        # simplejwt token blacklist (if present)
        "corsheaders",
    }
)


def _get_business_app_labels():
    """Return app labels for every installed app that is NOT a system app."""
    return {
        cfg.label
        for cfg in django_apps.get_app_configs()
        if cfg.label not in SYSTEM_APP_LABELS
    }


def _get_deletion_ordered_models(app_labels):
    """
    Collect every *concrete, managed* model from the given app labels
    and return them ordered so that FK/O2O children come before parents.

    This ensures sequential ``.objects.all().delete()`` never violates
    a foreign-key constraint (even without DB-level CASCADE).
    """
    all_models = []
    for cfg in django_apps.get_app_configs():
        if cfg.label in app_labels:
            for model in cfg.get_models():
                if model._meta.proxy or not model._meta.managed:
                    continue
                all_models.append(model)

    model_set = set(all_models)

    # parent → {children}  (child has FK/O2O pointing at parent)
    children_of = {m: set() for m in all_models}
    for model in all_models:
        for field in model._meta.get_fields():
            if isinstance(field, (db_models.ForeignKey, db_models.OneToOneField)):
                parent = field.related_model
                if parent in model_set and parent is not model:
                    children_of[parent].add(model)

    # DFS post-order — children are appended before their parent.
    visited = set()
    order = []

    def _dfs(m):
        if m in visited:
            return
        visited.add(m)
        for child in children_of[m]:
            _dfs(child)
        order.append(m)

    for m in all_models:
        _dfs(m)

    return order


def perform_system_reset(superuser_id, using="default"):
    """
    Delete **all** business / domain data while preserving:

    * Django system tables (auth groups, permissions, content types, sessions …)
    * The super-admin account identified by *superuser_id*

    The entire operation runs inside one atomic transaction so a
    partial failure leaves the database untouched.
    """
    from accounts.models import User  # local import to avoid circular refs
    from sync.models import CachedCredential

    app_labels = _get_business_app_labels()
    ordered = _get_deletion_ordered_models(app_labels)

    with transaction.atomic(using=using):
        for model in ordered:
            if model is User:
                # Keep the requesting super-admin; remove everyone else
                model.objects.using(using).exclude(id=superuser_id).delete()
            elif model is CachedCredential:
                # Preserve the requesting superuser's offline credential mirror.
                model.objects.using(using).exclude(user_id=superuser_id).delete()
            else:
                model.objects.using(using).all().delete()
