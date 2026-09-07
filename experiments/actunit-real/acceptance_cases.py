"""Independent acceptance criteria for the real ActUnit runtime repair."""
from pathlib import Path
import pytest
from pydantic import BaseModel, ConfigDict, Field
from actunit import ActionCall, ActionDefinition, ActionExecutionError, ActionRegistry, ActionRuntime, ExecutionContext

class Args(BaseModel):
    model_config = ConfigDict(extra='forbid')
    count: int = 3
    label: str | None = None
    tags: list[str] = Field(default_factory=list)

def invoke(handler, arguments=None, allowed=True):
    registry=ActionRegistry()
    registry.register(ActionDefinition('sample', 'sample action', Args, handler))
    call=ActionCall(id='real-regression', name='sample', arguments=arguments or {})
    context=ExecutionContext(Path.cwd(), frozenset({'sample'}) if allowed else frozenset())
    return ActionRuntime(registry).execute(call, context)

@pytest.mark.parametrize('arguments,expected', [
    ({}, {'count':3,'label':None,'tags':[]}),
    ({'count':0,'label':'x','tags':['a']}, {'count':0,'label':'x','tags':['a']}),
    ({'count':'4'}, {'count':4,'label':None,'tags':[]}),
])
def test_defaults_and_explicit_values_reach_handler(arguments, expected):
    result=invoke(lambda args, ctx: {'seen':args}, arguments)
    assert result.status=='ok'
    assert result.data['seen']==expected

@pytest.mark.parametrize('error', [RuntimeError('broken'), KeyError('missing'), TypeError('bad handler')])
def test_ordinary_handler_exceptions_are_structured(error):
    def handler(args, ctx): raise error
    result=invoke(handler)
    assert result.status=='error'
    assert result.error.code=='EXECUTION_FAILED'
    assert result.call_id=='real-regression'
    assert result.tool_name=='sample'
    assert result.duration_ms>=0

@pytest.mark.parametrize('value', [None, [], 'oops', 12, {'status':[]}, {'status':{}}, {'status':None}, {'status':'unknown'}])
def test_malformed_handler_results_are_structured(value):
    result=invoke(lambda args, ctx: value)
    assert result.status=='error'
    assert result.error.code=='INVALID_RESULT'

@pytest.mark.parametrize('error', [KeyboardInterrupt(), SystemExit(2)])
def test_process_control_exceptions_still_propagate(error):
    def handler(args, ctx): raise error
    with pytest.raises(type(error)): invoke(handler)

@pytest.mark.parametrize('error,code,status', [
    (ActionExecutionError('rejected',code='APPROVAL_DENIED'),'APPROVAL_DENIED','denied'),
    (ActionExecutionError('custom',code='CUSTOM'),'CUSTOM','error'),
    (FileNotFoundError('missing'),'FILE_NOT_FOUND','error'),
    (OSError('io'),'EXECUTION_FAILED','error'),
])
def test_existing_error_codes_are_preserved(error,code,status):
    def handler(args, ctx): raise error
    result=invoke(handler)
    assert result.status==status
    assert result.error.code==code

@pytest.mark.parametrize('status', ['ok','partial','error'])
def test_existing_result_statuses_and_data_survive(status):
    data={'status':status,'message':'result','exit_code':1 if status=='error' else 0}
    result=invoke(lambda args, ctx: data)
    assert result.status==status
    assert result.data==data
    assert result.call_id=='real-regression'

def test_denied_and_invalid_calls_never_execute():
    calls=[]
    def handler(args, ctx): calls.append(args); return {}
    assert invoke(handler, allowed=False).status=='denied'
    assert invoke(handler, {'workspace':'/'}).error.code=='INVALID_ARGUMENTS'
    assert not calls

def test_factory_defaults_are_isolated_between_invocations():
    seen=[]
    def handler(args, ctx):
        seen.append(list(args['tags']))
        args['tags'].append('changed')
        return {}
    invoke(handler)
    invoke(handler)
    assert seen==[[],[]]
