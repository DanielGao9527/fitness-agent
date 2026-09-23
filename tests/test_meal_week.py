import pytest
from fastapi.testclient import TestClient

from app import create_app
from test_foundation import DAY, application, client, register, meal


def week(client, day=DAY):
    response = client.get('/api/meals/week', params={'day': day})
    assert response.status_code == 200, response.text
    return response.json()


def test_authentication_and_invalid_date(client):
    assert client.get('/api/meals/week', params={'day': DAY}).status_code == 401
    register(client)
    for day in ('bad', '2026-02-30', '10000-01-01'):
        assert client.get('/api/meals/week', params={'day': day}).status_code == 422


@pytest.mark.parametrize('day,start,end,size', [
    (DAY, '2026-09-14', '2026-09-20', 7),
    ('2025-12-31', '2025-12-29', '2026-01-04', 7),
    ('0001-01-01', '0001-01-01', '0001-01-07', 7),
    ('9999-12-31', '9999-12-27', '9999-12-31', 5),
])
def test_empty_boundaries(client, day, start, end, size):
    register(client)
    result = week(client, day)
    assert (result['start'], result['end'], len(result['days'])) == (start, end, size)
    assert result['totals']['nutrition']['kcal']['known_total'] is None
    assert result['totals']['recorded_days'] == 0


def test_read_isolated_persistent_and_updates(client, application):
    register(client)
    body = meal(kcal_per_100g=60, source='Synthetic label')
    created = client.post('/api/meals', json=body).json()
    assert client.post('/api/meals', json=body).status_code == 201
    assert client.post('/api/meals', json=meal(name='Unknown')).status_code == 201
    result = week(client)
    assert result['totals']['meal_count'] == 2
    assert result['totals']['nutrition']['kcal'] == {'known_total': 150, 'missing_count': 1}
    assert result['totals']['estimated_nutrition']['kcal']['unknown_count'] == 1
    for row in result['days']:
        summary = client.get('/api/summary', params={'day': row['day']}).json()
        assert row['nutrition'] == summary['nutrition']
        assert row['estimated_nutrition'] == summary['estimated_nutrition']
    with TestClient(create_app(application.state.settings)) as restarted:
        restarted.cookies.update(client.cookies)
        assert week(restarted) == result
        restarted.cookies.clear()
        register(restarted, 'bob')
        assert week(restarted)['totals']['meal_count'] == 0
    changed = {key: value for key, value in created.items() if key != 'id'}
    changed['day'] = '2026-09-22'
    assert client.put(f"/api/meals/{created['id']}", json=changed).status_code == 200
    assert week(client)['totals']['meal_count'] == 1
    assert client.delete(f"/api/meals/{created['id']}").status_code == 204
    assert week(client, '2026-09-22')['totals']['meal_count'] == 0
    with application.state.database.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM ai_usage').fetchone()[0] == 0
