"""Mechanical pilot migration of FlaskBB registration; never used to repair trial output."""
import ast
import json
from pathlib import Path
import shutil
import sys

from ai_pod_cli.component_layout import validate_layout
from ai_pod_cli.pod.state import load_and_upgrade_plan

def bootstrap(source: Path, root: Path):
    shutil.copytree(source, root, ignore=shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache', '.venv'))
    def put(path, text):
        file=root/path;file.parent.mkdir(parents=True,exist_ok=True);file.write_text(text)
    for package in ['modules','modules/models','modules/providers','modules/providers/impl','modules/providers/public','modules/providers/contracts','modules/services','modules/services/impl','modules/services/public','modules/services/contracts','pipelines','interfaces','interfaces/registration']:
        put(package+'/__init__.py','')
    original=(root/'flaskbb/auth/services/registration.py').read_text()
    tree=ast.parse(original)
    classes={n.name:ast.get_source_segment(original,n) for n in tree.body if isinstance(n,ast.ClassDef)}
    put('modules/models/registration.py','from typing import Any\nfrom dataclasses import dataclass\nfrom ai_pod_cli import Model\n\nclass RegistrationEnvelope(Model):\n    info: Any\n\n@dataclass(init=True, repr=True, frozen=True, eq=False, order=False)\n'+classes['UsernameRequirements']+'\n')
    put('modules/providers/impl/registration.py','''from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from itertools import chain
import sqlalchemy as sa
from pytz import UTC
from flaskbb.extensions import db as default_db
from flaskbb.user.models import User
from flaskbb.core.exceptions import PersistenceError

_resources = ContextVar('registration_resources')

@contextmanager
def bind_registration(plugins, users, db):
    token = _resources.set((plugins, users, db))
    try:
        yield
    finally:
        _resources.reset(token)

class RegistrationResources:
    def validators(self):
        plugins, _, _ = _resources.get()
        return list(chain.from_iterable(plugins.hook.flaskbb_gather_registration_validators()))

    def report_failure(self, info, failures):
        plugins, _, _ = _resources.get()
        plugins.hook.flaskbb_registration_failure_handler(user_info=info, failures=failures)

    def store(self, info):
        _, _, db = _resources.get()
        try:
            user = User(username=info.username, email=info.email, password=info.password,
                        language=info.language, primary_group_id=info.group, date_joined=datetime.now(UTC))
            db.session.add(user)
            db.session.commit()
            return user
        except Exception as error:
            db.session.rollback()
            raise PersistenceError('Could not persist user') from error

    def post_process(self, user):
        plugins, _, _ = _resources.get()
        plugins.hook.flaskbb_registration_post_processor(user=user)

    @staticmethod
    def count_users(users, field, value):
        return default_db.session.execute(sa.select(sa.func.count(users.id)).filter(sa.func.lower(getattr(users, field)) == value)).scalar_one()
''')
    put('modules/providers/public/registration.py','from ..impl.registration import RegistrationResources as RegistrationResources, bind_registration as bind_registration\n')
    validators=[]
    for name in ['UsernameValidator','UsernameUniquenessValidator','EmailUniquenessValidator']:
        text=classes[name]
        if name!='UsernameValidator':
            field='username' if name.startswith('Username') else 'email'
            start=text.index('        count = db.session.execute(')
            end=text.index('        if count != 0:',start)
            text=text[:start]+f"        count = RegistrationResources.count_users(self.users, '{field}', user_info.{field})\n"+text[end:]
        validators.append(text)
    put('modules/services/impl/validators.py','import typing as t\nfrom flask_babelplus import gettext as _\nfrom flaskbb.core.auth.registration import UserValidator, UserRegistrationInfo\nfrom flaskbb.core.exceptions import ValidationError\nfrom flaskbb.user.models import User\nfrom modules.models.registration import UsernameRequirements\nfrom modules.providers.public.registration import RegistrationResources\n\n'+'\n\n'.join(validators)+'\n')
    put('modules/services/public/validators.py','from ..impl.validators import UsernameValidator as UsernameValidator, UsernameUniquenessValidator as UsernameUniquenessValidator, EmailUniquenessValidator as EmailUniquenessValidator\n')
    definitions={
        'ValidateRegistration':('validate',{'envelope':'any'},{'failures':'any','native_error':'any'},'''        failures = []
        try:
            for validator in self.resources.validators():
                try:
                    validator(ctx.get('envelope').info)
                except ValidationError as error:
                    failures.append((error.attribute, error.reason))
            return {'failures': failures, 'native_error': None}
        except Exception as error:
            return {'failures': failures, 'native_error': error}
'''),
        'PersistRegistration':('persist',{'envelope':'any'},{'user':'any','native_error':'any'},'''        try:
            return {'user': self.resources.store(ctx.get('envelope').info), 'native_error': None}
        except Exception as error:
            return {'user': None, 'native_error': error}
'''),
        'PostprocessRegistration':('postprocess',{'user':'any'},{'native_error':'any'},'''        try:
            self.resources.post_process(ctx.get('user'))
            return {'native_error': None}
        except Exception as error:
            return {'native_error': error}
'''),
        'ReportRegistrationFailure':('failure',{'envelope':'any','failures':'any'},{'native_error':'any'},'''        try:
            self.resources.report_failure(ctx.get('envelope').info, ctx.get('failures'))
            return {'native_error': None}
        except Exception as error:
            return {'native_error': error}
''')}
    beans=[dict(id='RegistrationEnvelope',category='model',class_path='modules.models.registration.RegistrationEnvelope',dependencies=[],inputs={},outputs={}),dict(id='RegistrationResources',category='provider',class_path='modules.providers.public.registration.RegistrationResources',dependencies=[],inputs={},outputs={},methods={})]
    for name,(file,inputs,outputs,body) in definitions.items():
        put(f'modules/services/impl/{file}.py',f'from injector import inject\nfrom ai_pod_cli import PipelineContext\nfrom modules.providers.public.registration import RegistrationResources\nfrom flaskbb.core.exceptions import ValidationError\n\nclass {name}:\n    @inject\n    def __init__(self, resources: RegistrationResources):\n        self.resources = resources\n\n    def execute(self, ctx: PipelineContext):\n'+body)
        put(f'modules/services/public/{file}.py',f'from ..impl.{file} import {name} as {name}\n')
        beans.append(dict(id=name,category='service',class_path=f'modules.services.public.{file}.{name}',dependencies=['RegistrationResources'],inputs=inputs,outputs=outputs,methods={}))
    put('pipelines/registration.py','''from ai_pod_cli.config import load_beans
from ai_pod_cli.container import Pod, build_container
from flaskbb.core.exceptions import StopValidation
from modules.services.public.validate import ValidateRegistration
from modules.services.public.persist import PersistRegistration
from modules.services.public.postprocess import PostprocessRegistration
from modules.services.public.failure import ReportRegistrationFailure

def run(ctx):
    S = Pod(build_container(load_beans()))
    S(ValidateRegistration).execute_all(ctx)
    error = ctx.get('native_error')
    if error is not None and not isinstance(error, StopValidation):
        raise error
    if isinstance(error, StopValidation):
        ctx.set('failures', error.reasons)
    if ctx.get('failures') or isinstance(error, StopValidation):
        error = error or StopValidation(ctx.get('failures'))
        S(ReportRegistrationFailure).execute_all(ctx)
        if ctx.get('native_error') is not None:
            raise ctx.get('native_error')
        raise error
    S(PersistRegistration).execute_all(ctx)
    if ctx.get('native_error') is not None:
        raise ctx.get('native_error')
    S(PostprocessRegistration).execute_all(ctx)
    if ctx.get('native_error') is not None:
        raise ctx.get('native_error')
    return {'user': ctx.get('user')}
''')
    put('interfaces/registration/adapter.py','''from pathlib import Path
from ai_pod_cli.interface import InterfaceAdapter, create_context, load_manifest
from flaskbb.core.auth.registration import UserRegistrationService
from modules.models.registration import RegistrationEnvelope
from modules.providers.public.registration import bind_registration

class RegistrationAdapter(InterfaceAdapter, UserRegistrationService):
    def __init__(self, plugins=None, users=None, db=None):
        self.plugins, self.users, self.db = plugins, users, db

    def required_routes(self):
        return ['register_user']

    def smoke_payloads(self):
        return {'register_user': {'envelope': None}}

    def start(self, context, payload=None):
        return context.run_route('register_user', payload or {})

    def register(self, user_info):
        root = Path(__file__).resolve().parents[2]
        _, manifest = load_manifest('registration', root)
        with bind_registration(self.plugins, self.users, self.db):
            result = self.start(create_context(manifest, root), {'envelope': RegistrationEnvelope(info=user_info)})
        return result['user']
''')
    # Preserve unrelated post-processors and public import paths as host compatibility shims.
    removed={'UsernameRequirements','UsernameValidator','UsernameUniquenessValidator','EmailUniquenessValidator','RegistrationService'}
    lines=original.splitlines(keepends=True)
    for n in sorted([n for n in tree.body if isinstance(n,ast.ClassDef) and n.name in removed],key=lambda n:n.lineno,reverse=True):
        start=min([n.lineno]+[d.lineno for d in n.decorator_list])-1
        lines[start:n.end_lineno]=[]
    put('flaskbb/auth/services/registration.py',''.join(lines)+'\nfrom modules.models.registration import UsernameRequirements as UsernameRequirements\nfrom modules.services.public.validators import UsernameValidator as UsernameValidator, UsernameUniquenessValidator as UsernameUniquenessValidator, EmailUniquenessValidator as EmailUniquenessValidator\nfrom interfaces.registration.adapter import RegistrationAdapter as RegistrationService\n')
    put('beans_config.json',json.dumps({'beans':beans},indent=2)+'\n')
    put('routes.toml','[register_user]\npipeline = "pipelines/registration.py"\n')
    put('config.toml','# Host supplies FlaskBB resources per registration invocation.\n')
    put('interfaces/registration/interface.json',json.dumps(dict(name='registration',kind='web',adapter=dict(path='interfaces/registration/adapter.py',class_name='RegistrationAdapter'),artifacts=[],lifecycle={},support={'standalone':False,'host':'FlaskBB request/application context'}),indent=2))
    state=load_and_upgrade_plan(None,'Maintain FlaskBB registration behavior and preserve unrelated forum features')
    for v in state['stages'].values():v['status']='complete'
    state['agent']['status']='complete'
    put('aipod_plan.json',json.dumps(state,indent=2))
    errors=validate_layout(root,beans)
    if errors:raise RuntimeError(errors)
    put('MIGRATION.md','''# Evaluated boundary
Only registration and its validators are migrated. Models preserve legacy registration data;
Providers hold Flask/SQLAlchemy/plugin resources; four Services split validation, persistence,
post-processing and failure reporting; Pipeline owns their ordering and native exception propagation.
The existing FlaskBB RegistrationService import is a host adapter over the registered route.
Native plugin implementations and unrelated forum/auth features remain legacy. ORM user handles and
registration values use explicit `any` compatibility contracts. This is not a claim of full-project
AIPod migration or complete static capability control of plugins.
Run `python -m pytest tests -q` from this repository for the original regression suite.
The Interface requires a FlaskBB host context; verify through the actual application tests, not
an invented standalone app.py. Keep existing public classes, hook behavior and import paths compatible.
''')

if __name__=='__main__':bootstrap(Path(sys.argv[1]).resolve(),Path(sys.argv[2]).resolve())
