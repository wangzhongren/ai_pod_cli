"""Frozen external behavioral acceptance, shared by every arm; never supplied to workers."""
from dataclasses import replace
import os

import pytest
from pluggy import HookimplMarker
from flaskbb.auth.services.registration import (
    RegistrationService, UsernameUniquenessValidator, EmailUniquenessValidator,
)
from flaskbb.core.auth.registration import UserRegistrationInfo
from flaskbb.core.exceptions import StopValidation, ValidationError
from flaskbb.user.models import User, Group

pytestmark = pytest.mark.usefixtures("default_settings")
impl = HookimplMarker("flaskbb")
ROUND = int(os.environ.get("BENCHMARK_ROUND", "3"))
r2 = pytest.mark.skipif(ROUND < 2, reason="future requirement")
r3 = pytest.mark.skipif(ROUND < 3, reason="future requirement")


def info(**changes):
    return replace(UserRegistrationInfo("NewMember", "secret", "new@example.org", "en", 4), **changes)


class Hooks:
    def __init__(self, validator=None, failure_error=None, post_error=None):
        self.validator = validator
        self.failure_error = failure_error
        self.post_error = post_error
        self.validated, self.failures, self.posted = [], [], []

    @impl
    def flaskbb_gather_registration_validators(self):
        def validate(value):
            self.validated.append(value)
            if self.validator:
                self.validator(value)
        return [validate]

    @impl
    def flaskbb_registration_failure_handler(self, user_info, failures):
        self.failures.append((user_info, failures))
        if self.failure_error:
            raise self.failure_error

    @impl
    def flaskbb_registration_post_processor(self, user):
        self.posted.append(user)
        if self.post_error:
            raise self.post_error


def service(plugin_manager, database, hooks=None):
    if hooks:
        plugin_manager.register(hooks)
    return RegistrationService(plugins=plugin_manager, users=User, db=database)


@pytest.mark.parametrize("username,email", [(" NewMember ", " NEW@Example.Org "), ("\tNewMember\n", "\tNEW@EXAMPLE.ORG\n")])
def test_normalized_persistence(plugin_manager, database, default_groups, username, email):
    supplied = info(username=username, email=email)
    hooks = Hooks()
    user = service(plugin_manager, database, hooks).register(supplied)
    assert (user.username, user.email) == ("NewMember", "new@example.org")
    assert (supplied.username, supplied.email) == (username, email)
    assert len(hooks.validated) == 1
    normalized = hooks.validated[0]
    assert (normalized.username, normalized.email) == (user.username, user.email)
    assert normalized is not supplied
    assert (normalized.password, normalized.language, normalized.group) == (supplied.password, supplied.language, supplied.group)
    assert hooks.posted == [user]


def test_unchanged_identity_compatibility(plugin_manager, database, default_groups):
    supplied, hooks = info(), Hooks()
    service(plugin_manager, database, hooks).register(supplied)
    assert hooks.validated == [supplied]


def test_normalized_failure_hook(plugin_manager, database, default_groups):
    def reject(value):
        raise ValidationError("username", "rejected")
    supplied = info(username=" NewMember ", email=" NEW@Example.Org ")
    hooks = Hooks(validator=reject)
    with pytest.raises(StopValidation):
        service(plugin_manager, database, hooks).register(supplied)
    assert len(hooks.failures) == 1
    failed_info, reasons = hooks.failures[0]
    assert failed_info is hooks.validated[0]
    assert (failed_info.username, failed_info.email) == ("NewMember", "new@example.org")
    assert ("username", "rejected") in reasons
    assert not hooks.posted
    assert database.session.query(User).count() == 0


@pytest.mark.parametrize("username", ["FRED", " FrEd "])
def test_username_uniqueness_direct(database, Fred, username):
    with pytest.raises(ValidationError) as error:
        UsernameUniquenessValidator(User)(info(username=username))
    assert error.value.attribute == "username"


@pytest.mark.parametrize("email", ["FRED@FRED.FRED", " Fred@Fred.Fred "])
def test_email_uniqueness_direct(database, Fred, email):
    with pytest.raises(ValidationError) as error:
        EmailUniquenessValidator(User)(info(email=email))
    assert error.value.attribute == "email"


@r2
@pytest.mark.parametrize("group", [1, 2, 3, 5, 6, 999, None])
def test_group_rejection(plugin_manager, database, default_groups, group):
    hooks = Hooks()
    with pytest.raises(StopValidation) as error:
        service(plugin_manager, database, hooks).register(info(group=group))
    assert any(attribute == "group" for attribute, _ in error.value.reasons)
    assert len(hooks.failures) == 1
    assert hooks.failures[0][1] == error.value.reasons
    assert database.session.query(User).count() == 0
    assert hooks.posted == []


@r2
def test_group_guard_without_plugins(plugin_manager, database, default_groups):
    with pytest.raises(StopValidation) as error:
        service(plugin_manager, database).register(info(group=1))
    assert any(attribute == "group" for attribute, _ in error.value.reasons)
    assert database.session.query(User).count() == 0


@r2
def test_custom_ordinary_group_accepted(plugin_manager, database, default_groups):
    group = Group(name="New ordinary members")
    group.save()
    user = service(plugin_manager, database).register(info(group=group.id, username=" Trimmed ", email=" CAPS@Example.Org "))
    assert user.primary_group_id == group.id
    assert (user.username, user.email) == ("Trimmed", "caps@example.org")


@r2
def test_group_flags_not_ids(plugin_manager, database, default_groups):
    default_groups[3].admin = True
    database.session.commit()
    with pytest.raises(StopValidation) as error:
        service(plugin_manager, database).register(info())
    assert any(attribute == "group" for attribute, _ in error.value.reasons)
    assert database.session.query(User).count() == 0


@r3
@pytest.mark.parametrize("failure_error", [RuntimeError("plugin runtime failure"), ValueError("plugin bad value")])
@pytest.mark.parametrize("direct", [True, False])
def test_original_validation_survives_broken_handler(plugin_manager, database, default_groups, failure_error, direct):
    original = StopValidation([("username", "original rejection")])
    def reject(value):
        if direct:
            raise original
        raise ValidationError("username", "original rejection")
    hooks = Hooks(validator=reject, failure_error=failure_error)
    with pytest.raises(StopValidation) as error:
        service(plugin_manager, database, hooks).register(info())
    assert error.value.reasons == original.reasons
    if direct:
        assert error.value is original
    assert len(hooks.failures) == 1
    assert hooks.posted == []
    assert database.session.query(User).count() == 0


@r3
@pytest.mark.parametrize("failure_error", [KeyboardInterrupt(), SystemExit(7)])
def test_control_exceptions_propagate(plugin_manager, database, default_groups, failure_error):
    def reject(value):
        raise ValidationError("username", "no")
    hooks = Hooks(validator=reject, failure_error=failure_error)
    with pytest.raises(type(failure_error)) as error:
        service(plugin_manager, database, hooks).register(info())
    assert error.value is failure_error
    assert len(hooks.failures) == 1
    assert not hooks.posted
    assert database.session.query(User).count() == 0


@r3
def test_group_rejection_survives_broken_handler(plugin_manager, database, default_groups):
    hooks = Hooks(failure_error=RuntimeError("broken group failure hook"))
    with pytest.raises(StopValidation) as error:
        service(plugin_manager, database, hooks).register(info(group=1))
    assert any(attribute == "group" for attribute, _ in error.value.reasons)
    assert len(hooks.failures) == 1
    assert not hooks.posted
    assert database.session.query(User).count() == 0


@r3
def test_postprocessor_error_unchanged(plugin_manager, database, default_groups):
    original = RuntimeError("postprocessor failure remains caller-visible")
    hooks = Hooks(post_error=original)
    with pytest.raises(RuntimeError) as error:
        service(plugin_manager, database, hooks).register(info())
    assert error.value is original
    assert len(hooks.posted) == 1
    assert hooks.failures == []
    assert database.session.query(User).count() == 1
