import time

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
import pytest

from text2splat.app import create_app
from text2splat.settings import AgentSettings
from test_chat import ConversationModel
from test_workflow import FakeTools, FakeModel, action


@pytest.fixture
def chat_client(tmp_path):
    model = ConversationModel()
    app = create_app(AgentSettings(data_dir=tmp_path, poll_interval=.01), FakeTools(), FakeModel(), chat_model=model)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        yield client, model


def finish(client, turn):
    for _ in range(100):
        result = client.get('/api/graphics/chat/turns/' + turn['id']).json()
        if result['status'] in {'COMPLETED', 'FAILED'}:
            return result
        time.sleep(.01)
    raise AssertionError('Chat did not finish')


def test_chat_api_recovery_idempotency_and_history(chat_client):
    client, model = chat_client
    route = '/api/graphics/chat'
    args = dict(json={'message': '你好'}, headers={'Idempotency-Key': 'hello'})
    response = client.post(route, **args)
    assert response.status_code == 202
    first = finish(client, response.json())
    assert first['assistant'] and first['jobId'] is None
    assert client.post(route, **args).json() == first and len(model.history) == 1
    assert client.get(route + '/requests/hello').json() == first
    assert client.get(route + '/requests/absent').status_code == 404
    assert client.post(route, json={'message': '改了'}, headers=args['headers']).status_code == 400
    response = client.get('/api/graphics/chats/' + first['conversationId'])
    assert response.json()['turns'] == [first] and response.json()['latestJob'] is None
    assert response.headers['cache-control'] == 'no-store'
    assert client.get('/api/graphics/jobs').json() == []
    model.replies = [action('generate_3d_model', {'description': '生成一个青瓷花瓶'})]
    second = finish(client, client.post(route, json={'message': '生成青瓷花瓶',
                    'conversationId': first['conversationId']}).json())
    assert second['jobId'] and second['delegation']['tool'] == 'generate_3d_model'
    conversation = client.get('/api/graphics/chats/' + first['conversationId']).json()
    assert len(conversation['turns']) == 2 and conversation['latestJob']['id'] == second['jobId']
    assert client.get('/api/graphics/health').json()['agents'] == ['chat_supervisor', '3d_react_specialist']


@pytest.mark.parametrize('body', [{'message': ' '}, {'message': 'a' * 2001}, {'message': '你好', 'conversationId': '../x'}])
def test_invalid_chat_input_has_no_side_effects(chat_client, body):
    client, model = chat_client
    assert client.post('/api/graphics/chat', json=body).status_code == 400
    assert not model.history and client.get('/api/graphics/jobs').json() == []


def test_chat_same_origin_and_unknown_conversation(chat_client):
    client, model = chat_client
    assert client.post('/api/graphics/chat', json={'message': '你好'}, headers={'origin': 'https://evil.example'}).status_code == 403
    assert client.post('/api/graphics/chat', json={'message': '你好', 'conversationId': 'f' * 32}).status_code == 404
    assert client.get('/api/graphics/chats/' + 'f' * 32).status_code == 404
    assert not model.history


def test_chat_without_key_reports_configuration_and_never_falls_back_to_generation(tmp_path):
    app = create_app(AgentSettings(data_dir=tmp_path, zhipu_key=''), FakeTools(), FakeModel())
    with TestClient(app, base_url='http://127.0.0.1') as client:
        turn = finish(client, client.post('/api/graphics/chat', json={'message': '你好'}).json())
        assert turn['status'] == 'FAILED' and 'ZHIPU_API_KEY' in turn['assistant']
        assert client.get('/api/graphics/jobs').json() == []
