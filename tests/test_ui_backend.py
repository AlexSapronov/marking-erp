from fastapi.testclient import TestClient
from marking_erp.config import Settings
from marking_erp.web import create_app


def test_shell_and_packaged_assets_do_not_require_a_database():
    app=create_app(Settings(database_url="postgresql+psycopg://unused@127.0.0.1:1/test"))
    with TestClient(app) as client:
        for path in ['/', '/login', '/profile', '/users','/groups','/rules','/codes','/operations','/system','/audit']:
            response=client.get(path)
            assert response.status_code==200 and 'lang="ru"' in response.text
            assert 'password_hash' not in response.text
            assert "frame-ancestors 'none'" in response.headers['content-security-policy']
        for path in ['/assets/app.js','/assets/theme.js','/assets/app.css','/assets/registry.js','/assets/admin.js','/assets/operations.js','/assets/system.js','/assets/audit.js']:
            response=client.get(path)
            assert response.status_code==200 and response.headers['cache-control']=='no-store'
        assert client.get('/assets/../auth.py').status_code==404

