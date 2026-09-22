"""Doc/01_개발정의서.md §11에서 "결정: 지원"으로 확정한 세 항목에 대한 단위
테스트 — 실제 서버 없이 Driver 생성자 호출 인자를 가로채 검증한다.

1. Elasticsearch/OpenSearch: 별도 Adapter(elasticsearch.py/opensearch.py)로
   분리되어 각자 자기 Client 라이브러리만 사용하는지.
2. Redis Cluster: cluster_mode 설정에 따라 RedisCluster로 분기하고,
   CLIENT LIST/SLOWLOG GET/INFO처럼 리스트·dict를 반환하는 명령이 전체
   노드 결과를 병합하는지, SCAN이 scan_iter 기반으로 동작하는지.
3. MongoDB Replica Set/mongodb+srv://: connection.uri가 있으면 그대로
   MongoClient에 전달되는지, 없으면 기존 uri_host/port 방식이 유지되는지.
"""
from __future__ import annotations

import sys
import types

# ----------------------------------------------------------------------
# 1. Elasticsearch / OpenSearch 분리
# ----------------------------------------------------------------------


def test_elasticsearch_and_opensearch_are_separate_adapter_classes():
    from server.database.elasticsearch import ElasticsearchAdapter
    from server.database.opensearch import OpenSearchAdapter
    from server.database.registry import AdapterRegistry, register_builtin_adapters

    register_builtin_adapters()
    assert ElasticsearchAdapter is not OpenSearchAdapter
    assert AdapterRegistry.resolve("elasticsearch") is ElasticsearchAdapter
    assert AdapterRegistry.resolve("opensearch") is OpenSearchAdapter


def test_elasticsearch_adapter_uses_elasticsearch_py_client(monkeypatch):
    import server.database.elasticsearch as es_module

    captured: dict = {}

    class FakeElasticsearch:
        def __init__(self, hosts, basic_auth=None, request_timeout=None):
            captured["hosts"] = hosts
            captured["request_timeout"] = request_timeout

    fake_module = types.SimpleNamespace(Elasticsearch=FakeElasticsearch)
    monkeypatch.setitem(sys.modules, "elasticsearch", fake_module)

    adapter = es_module.ElasticsearchAdapter(
        {
            "connection": {"host": "es.internal", "port": 9200},
            "query_timeout_sec": 20,
        }
    )
    adapter._ensure_client()

    assert captured["hosts"] == [{"host": "es.internal", "port": 9200}]
    assert captured["request_timeout"] == 20


def test_opensearch_adapter_uses_opensearch_py_client(monkeypatch):
    import server.database.opensearch as opensearch_module

    captured: dict = {}

    class FakeOpenSearch:
        def __init__(self, hosts, http_auth=None, timeout=None, use_ssl=False):
            captured["hosts"] = hosts
            captured["timeout"] = timeout
            captured["use_ssl"] = use_ssl

    fake_module = types.SimpleNamespace(OpenSearch=FakeOpenSearch)
    monkeypatch.setitem(sys.modules, "opensearchpy", fake_module)

    adapter = opensearch_module.OpenSearchAdapter(
        {
            "connection": {"host": "os.internal", "port": 9200},
            "query_timeout_sec": 25,
        }
    )
    adapter._ensure_client()

    assert captured["hosts"] == [{"host": "os.internal", "port": 9200}]
    assert captured["timeout"] == 25


# ----------------------------------------------------------------------
# 2. Redis Cluster
# ----------------------------------------------------------------------


def test_redis_adapter_defaults_to_single_instance_client(monkeypatch):
    import server.database.redis as redis_module

    captured: dict = {}

    class FakeRedis:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_redis_pkg = types.SimpleNamespace(Redis=FakeRedis)
    monkeypatch.setitem(sys.modules, "redis", fake_redis_pkg)

    adapter = redis_module.RedisAdapter(
        {"connection": {"host": "cache.internal", "port": 6379}}
    )
    adapter._ensure_client()

    assert captured["host"] == "cache.internal"
    assert adapter._is_cluster is False


def test_redis_adapter_uses_rediscluster_when_cluster_mode_enabled(monkeypatch):
    import server.database.redis as redis_module

    captured: dict = {}

    class FakeClusterNode:
        def __init__(self, host, port):
            self.host = host
            self.port = port

    class FakeRedisCluster:
        ALL_NODES = "all-nodes-sentinel"

        def __init__(self, startup_nodes, **kwargs):
            captured["startup_nodes"] = startup_nodes
            captured.update(kwargs)

    fake_cluster_module = types.SimpleNamespace(
        RedisCluster=FakeRedisCluster, ClusterNode=FakeClusterNode
    )
    monkeypatch.setitem(sys.modules, "redis.cluster", fake_cluster_module)

    adapter = redis_module.RedisAdapter(
        {
            "connection": {
                "cluster_mode": True,
                "nodes": [
                    {"host": "node-0001.internal", "port": 6379},
                    {"host": "node-0002.internal", "port": 6379},
                ],
            }
        }
    )
    client = adapter._ensure_client()

    assert adapter._is_cluster is True
    assert isinstance(client, FakeRedisCluster)
    assert [n.host for n in captured["startup_nodes"]] == [
        "node-0001.internal",
        "node-0002.internal",
    ]


class _FakeClusterClient:
    """redis-py의 RedisCluster처럼 target_nodes=ALL_NODES일 때 노드별
    결과를 dict로 돌려주는 최소 스텁."""

    ALL_NODES = "all-nodes-sentinel"

    def client_list(self, target_nodes=None):
        assert target_nodes == self.ALL_NODES
        return {
            "node-a": [{"id": 1, "cmd": "get"}],
            "node-b": [{"id": 2, "cmd": "set"}],
        }

    def slowlog_get(self, limit, target_nodes=None):
        assert target_nodes == self.ALL_NODES
        return {
            "node-a": [{"id": 10, "duration": 5000, "command": [b"GET", b"k1"]}],
            "node-b": [],
        }

    def info(self, section, target_nodes=None):
        assert target_nodes == self.ALL_NODES
        return {
            "node-a": {"errorstat_ERR": 3, "some_text": "a"},
            "node-b": {"errorstat_ERR": 2, "some_text": "b"},
        }


def test_all_nodes_list_merges_client_list_across_cluster_nodes(monkeypatch):
    import server.database.redis as redis_module

    fake_cluster_module = types.SimpleNamespace(RedisCluster=_FakeClusterClient)
    monkeypatch.setitem(sys.modules, "redis.cluster", fake_cluster_module)

    adapter = redis_module.RedisAdapter({"connection": {"cluster_mode": True}})
    merged = adapter._all_nodes_list(_FakeClusterClient(), "client_list")

    assert {e["id"] for e in merged} == {1, 2}


def test_all_nodes_dict_sum_sums_numeric_values_across_cluster_nodes(monkeypatch):
    import server.database.redis as redis_module

    fake_cluster_module = types.SimpleNamespace(RedisCluster=_FakeClusterClient)
    monkeypatch.setitem(sys.modules, "redis.cluster", fake_cluster_module)

    adapter = redis_module.RedisAdapter({"connection": {"cluster_mode": True}})
    merged = adapter._all_nodes_dict_sum(_FakeClusterClient(), "errorstats")

    assert merged["errorstat_ERR"] == 5  # 3 + 2 노드 합산
    assert merged["some_text"] == "b"  # 숫자가 아닌 값은 마지막 노드 값으로 덮어씀


def test_all_nodes_list_calls_method_directly_when_not_cluster():
    import server.database.redis as redis_module

    adapter = redis_module.RedisAdapter({"connection": {}})

    class FakeSingleClient:
        def client_list(self):
            return [{"id": 1}]

    result = adapter._all_nodes_list(FakeSingleClient(), "client_list")
    assert result == [{"id": 1}]


def test_scan_pattern_uses_scan_iter_and_respects_max_keys():
    import server.database.redis as redis_module

    adapter = redis_module.RedisAdapter({"connection": {}})

    class FakeScanClient:
        def scan_iter(self, match, count):
            assert match == "user:*"
            for i in range(50):
                yield f"user:{i}"

    keys = adapter._scan_pattern(FakeScanClient(), "user:*", max_keys=5)
    assert keys == [f"user:{i}" for i in range(5)]


# ----------------------------------------------------------------------
# 3. MongoDB Replica Set / mongodb+srv://
# ----------------------------------------------------------------------


def test_mongodb_adapter_uses_uri_host_port_when_no_uri_given(monkeypatch):
    import server.database.mongodb as mongo_module

    captured: dict = {}

    class FakeMongoClient:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    fake_pymongo = types.SimpleNamespace(MongoClient=FakeMongoClient)
    monkeypatch.setitem(sys.modules, "pymongo", fake_pymongo)

    adapter = mongo_module.MongoDBAdapter(
        {
            "connection": {"uri_host": "mongo.internal", "port": 27017, "database": "d"},
            "user": "u",
            "password": "p",
        }
    )
    adapter._ensure_client()

    assert captured["kwargs"]["host"] == "mongo.internal"
    assert captured["kwargs"]["port"] == 27017


def test_mongodb_adapter_uses_full_uri_when_configured(monkeypatch):
    import server.database.mongodb as mongo_module

    captured: dict = {}

    class FakeMongoClient:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    fake_pymongo = types.SimpleNamespace(MongoClient=FakeMongoClient)
    monkeypatch.setitem(sys.modules, "pymongo", fake_pymongo)

    replica_set_uri = "mongodb://host1,host2,host3/?replicaSet=rs0"
    adapter = mongo_module.MongoDBAdapter(
        {"connection": {"uri": replica_set_uri}, "user": "u", "password": "p"}
    )
    adapter._ensure_client()

    assert captured["args"] == (replica_set_uri,)
    assert "host" not in captured["kwargs"]


def test_mongodb_db_falls_back_to_default_database_when_no_database_key(monkeypatch):
    import server.database.mongodb as mongo_module

    class FakeDefaultDb:
        name = "default-from-uri"

    class FakeClient:
        def get_default_database(self):
            return FakeDefaultDb()

    fake_pymongo = types.SimpleNamespace(MongoClient=lambda *a, **kw: FakeClient())
    monkeypatch.setitem(sys.modules, "pymongo", fake_pymongo)

    adapter = mongo_module.MongoDBAdapter(
        {"connection": {"uri": "mongodb+srv://cluster0.example.mongodb.net/"}}
    )
    db = adapter._db()

    assert db.name == "default-from-uri"
