import os
import threading
from collections import deque
from unittest import TestCase

from mock import Mock, patch


TEST_DIR = os.path.dirname(os.path.realpath(__file__))
CONF_DIR = os.path.join(TEST_DIR, 'data', 'conf-directory')


def storageSchemas():
  from carbon.storage import PatternSchema, DefaultSchema, Archive
  return [
    PatternSchema('carbon', r'^carbon\.', [Archive.fromString('60:90d')]),
    PatternSchema('catchall', '.*', [Archive.fromString('60s:1d')]),
    DefaultSchema('default', [Archive.fromString('1s:1h')]),
  ]


def aggregationSchemas():
  from carbon.storage import PatternSchema, DefaultSchema
  return [
    PatternSchema('min', r'\.min$', (0.1, 'min')),
    PatternSchema('average', '.*', (0.5, 'average')),
    DefaultSchema('default', (None, None)),
  ]


class SchemaMatchCacheTest(TestCase):

  def setUp(self):
    self._settings_patch = patch.dict('carbon.conf.settings', {'CONF_DIR': CONF_DIR})
    self._settings_patch.start()
    from carbon import storage
    from carbon.writer import SchemaMatchCache
    self.storage = storage
    self.SchemaMatchCache = SchemaMatchCache
    self.cache = SchemaMatchCache(storageSchemas())

  def tearDown(self):
    self._settings_patch.stop()

  def test_first_match_wins(self):
    schema = self.cache.match('carbon.foo')
    self.assertEqual('carbon', schema.name)

  def test_default_schema_fallback(self):
    schema = self.cache.match('anything.else')
    self.assertEqual('catchall', schema.name)

  def test_match_result_is_cached(self):
    first = self.cache.match('carbon.bar')
    cached = self.cache.match('carbon.bar')
    self.assertIs(first, cached)
    self.assertIn('carbon.bar', self.cache._matches)

  def test_cached_match_does_not_rescan(self):
    self.cache.match('carbon.bar')
    with patch.object(self.cache, '_schemas') as schemas:
      self.assertEqual('carbon', self.cache.match('carbon.bar').name)
      schemas.__iter__.assert_not_called()

  def test_distinct_metrics_resolved_independently(self):
    self.assertEqual('carbon', self.cache.match('carbon.a').name)
    self.assertEqual('catchall', self.cache.match('stats.a').name)
    self.assertEqual('carbon', self.cache.match('carbon.b').name)

  def test_no_match_returns_none_and_is_cached(self):
    cache = self.SchemaMatchCache(
      [self.storage.PatternSchema('carbon', r'^carbon\.', [])])
    self.assertIsNone(cache.match('stats.miss'))
    with patch.object(cache, '_schemas') as schemas:
      self.assertIsNone(cache.match('stats.miss'))
      schemas.__iter__.assert_not_called()

  def test_replace_schemas_invalidates_cache(self):
    self.assertEqual('carbon', self.cache.match('carbon.x').name)

    new_schemas = [
      self.storage.PatternSchema('special', r'^carbon\.x$',
                                 [self.storage.Archive.fromString('10s:1h')]),
      self.storage.PatternSchema('carbon', r'^carbon\.',
                                 [self.storage.Archive.fromString('60:90d')]),
    ]
    self.cache.replace_schemas(new_schemas)

    self.assertEqual({}, self.cache._matches)
    self.assertIs(new_schemas, self.cache._schemas)
    self.assertEqual('special', self.cache.match('carbon.x').name)
    self.assertEqual('carbon', self.cache.match('carbon.y').name)

  def test_concurrent_matches_are_consistent(self):
    results = []

    def resolve():
      for _ in range(200):
        results.append(self.cache.match('carbon.thread').name)

    threads = [threading.Thread(target=resolve) for _ in range(4)]
    for thread in threads:
      thread.start()
    for thread in threads:
      thread.join()

    self.assertTrue(results)
    self.assertTrue(set(results).issubset({'carbon'}))

  def test_replace_while_matching_does_not_corrupt_cache(self):
    def churn_schemas():
      for _ in range(50):
        self.cache.replace_schemas(storageSchemas())

    def churn_matches():
      for _ in range(50):
        self.assertIn(self.cache.match('carbon.z').name,
                      ('carbon', 'catchall', 'default'))

    reloader = threading.Thread(target=churn_schemas)
    matcher = threading.Thread(target=churn_matches)
    reloader.start()
    matcher.start()
    reloader.join()
    matcher.join()


class SchemaReloadTest(TestCase):

  def setUp(self):
    self._settings_patch = patch.dict('carbon.conf.settings', {'CONF_DIR': CONF_DIR})
    self._settings_patch.start()
    import carbon.writer as writer
    self.writer = writer
    self._storage_cache = self.writer.STORAGE_SCHEMA_CACHE
    self._aggregation_cache = self.writer.AGGREGATION_SCHEMA_CACHE
    self.writer.STORAGE_SCHEMA_CACHE = self.writer.SchemaMatchCache(
      self._storage_cache._schemas)
    self.writer.AGGREGATION_SCHEMA_CACHE = self.writer.SchemaMatchCache(
      self._aggregation_cache._schemas)

  def tearDown(self):
    self.writer.STORAGE_SCHEMA_CACHE = self._storage_cache
    self.writer.AGGREGATION_SCHEMA_CACHE = self._aggregation_cache
    self._settings_patch.stop()

  def test_reload_storage_schemas_replaces_cache(self):
    writer = self.writer
    new_schemas = storageSchemas()
    cache = writer.STORAGE_SCHEMA_CACHE
    cache.match('carbon.a')

    with patch.object(writer, 'loadStorageSchemas', return_value=new_schemas):
      writer.reloadStorageSchemas()

    self.assertIs(writer.SCHEMAS, new_schemas)
    self.assertIs(cache._schemas, new_schemas)
    self.assertEqual({}, cache._matches)

  def test_failed_storage_reload_keeps_cache(self):
    writer = self.writer
    cache = writer.STORAGE_SCHEMA_CACHE
    old_schemas = cache._schemas
    cache.match('carbon.a')
    old_matches = dict(cache._matches)

    with patch.object(writer, 'loadStorageSchemas',
                      side_effect=Exception('boom')):
      writer.reloadStorageSchemas()

    self.assertIs(old_schemas, cache._schemas)
    self.assertEqual(old_matches, cache._matches)

  def test_reload_aggregation_schemas_replaces_cache(self):
    writer = self.writer
    new_schemas = aggregationSchemas()
    cache = writer.AGGREGATION_SCHEMA_CACHE
    cache.match('carbon.a')

    with patch.object(writer, 'loadAggregationSchemas', return_value=new_schemas):
      writer.reloadAggregationSchemas()

    self.assertIs(writer.AGGREGATION_SCHEMAS, new_schemas)
    self.assertIs(cache._schemas, new_schemas)
    self.assertEqual({}, cache._matches)

  def test_failed_aggregation_reload_keeps_cache(self):
    writer = self.writer
    cache = writer.AGGREGATION_SCHEMA_CACHE
    old_schemas = cache._schemas
    cache.match('carbon.a')
    old_matches = dict(cache._matches)

    with patch.object(writer, 'loadAggregationSchemas',
                      side_effect=Exception('boom')):
      writer.reloadAggregationSchemas()

    self.assertIs(old_schemas, cache._schemas)
    self.assertEqual(old_matches, cache._matches)


class WriteCachedDataPointsTest(TestCase):

  def setUp(self):
    self._settings_patch = patch.dict('carbon.conf.settings', {'CONF_DIR': CONF_DIR})
    self._settings_patch.start()
    import carbon.writer as writer
    self.writer = writer
    self._storage_cache = writer.STORAGE_SCHEMA_CACHE
    self._aggregation_cache = writer.AGGREGATION_SCHEMA_CACHE
    self._create_bucket = writer.CREATE_BUCKET
    self._update_bucket = writer.UPDATE_BUCKET
    writer.STORAGE_SCHEMA_CACHE = writer.SchemaMatchCache(storageSchemas())
    writer.AGGREGATION_SCHEMA_CACHE = writer.SchemaMatchCache(aggregationSchemas())
    writer.CREATE_BUCKET = None
    writer.UPDATE_BUCKET = None

  def tearDown(self):
    self.writer.STORAGE_SCHEMA_CACHE = self._storage_cache
    self.writer.AGGREGATION_SCHEMA_CACHE = self._aggregation_cache
    self.writer.CREATE_BUCKET = self._create_bucket
    self.writer.UPDATE_BUCKET = self._update_bucket
    self._settings_patch.stop()

  def _runWriterWithCache(self, cache, database):
    with patch.object(self.writer, 'MetricCache', return_value=cache), \
         patch.object(self.writer.state, 'database', new=database):
      self.writer.writeCachedDataPoints()

  def test_new_metric_uses_cached_schemas_to_create(self):
    from carbon.cache import _MetricCache
    cache = _MetricCache()
    cache['carbon.new'] = {1: 1.0}
    cache.new_metrics = deque(['carbon.new'])

    database = Mock()
    database.exists.return_value = False

    self._runWriterWithCache(cache, database)

    database.create.assert_called_once()
    metric, archiveConfig, xff, method = database.create.call_args[0]
    self.assertEqual('carbon.new', metric)
    self.assertEqual([(60, 90 * 24 * 60)], archiveConfig)
    self.assertEqual((0.5, 'average'), (xff, method))

    self.assertEqual('carbon',
                     self.writer.STORAGE_SCHEMA_CACHE.match('carbon.new').name)
    self.assertEqual('average',
                     self.writer.AGGREGATION_SCHEMA_CACHE.match('carbon.new').name)

  def test_second_create_reuses_cached_schema_match(self):
    from carbon.cache import _MetricCache
    database = Mock()
    database.exists.return_value = False

    storage_schemas = storageSchemas()
    aggregation_schemas = aggregationSchemas()
    all_schemas = storage_schemas + aggregation_schemas
    for schema in all_schemas:
      schema.matches = Mock(wraps=schema.matches)
    self.writer.STORAGE_SCHEMA_CACHE = self.writer.SchemaMatchCache(storage_schemas)
    self.writer.AGGREGATION_SCHEMA_CACHE = self.writer.SchemaMatchCache(aggregation_schemas)

    def runOnce():
      for schema in all_schemas:
        schema.matches.reset_mock()
      cache = _MetricCache()
      cache['carbon.repeat'] = {1: 1.0}
      cache.new_metrics = deque(['carbon.repeat'])
      self._runWriterWithCache(cache, database)
      return sum(schema.matches.call_count for schema in all_schemas)

    first_match_calls = runOnce()
    second_match_calls = runOnce()
    self.assertGreater(first_match_calls, 0)
    self.assertEqual(0, second_match_calls)

  def test_no_matching_storage_schema_raises(self):
    from carbon.cache import _MetricCache
    from carbon.storage import PatternSchema
    self.writer.STORAGE_SCHEMA_CACHE = self.writer.SchemaMatchCache(
      [PatternSchema('onlycarbon', r'^carbon\.', [])])
    cache = _MetricCache()
    cache['stats.orphan'] = {1: 1.0}
    cache.new_metrics = deque(['stats.orphan'])

    database = Mock()
    database.exists.return_value = False

    with patch.object(self.writer, 'MetricCache', return_value=cache), \
         patch.object(self.writer.state, 'database', new=database):
      self.assertRaises(Exception, self.writer.writeCachedDataPoints)

  def test_reload_takes_effect_for_new_metric(self):
    from carbon.cache import _MetricCache
    from carbon.storage import PatternSchema, DefaultSchema, Archive
    database = Mock()
    database.exists.return_value = False

    cache = _MetricCache()
    cache['carbon.reload'] = {1: 1.0}
    cache.new_metrics = deque(['carbon.reload'])
    self._runWriterWithCache(cache, database)
    first_archive = database.create.call_args[0][1]

    reloaded = [
      PatternSchema('special', r'^carbon\.reload$', [Archive.fromString('10s:1h')]),
      PatternSchema('carbon', r'^carbon\.', [Archive.fromString('60:90d')]),
      DefaultSchema('default', [Archive.fromString('60:1d')]),
    ]
    with patch.object(self.writer, 'loadStorageSchemas', return_value=reloaded):
      self.writer.reloadStorageSchemas()

    cache = _MetricCache()
    cache['carbon.reload'] = {2: 2.0}
    cache.new_metrics = deque(['carbon.reload'])
    self._runWriterWithCache(cache, database)
    second_archive = database.create.call_args[0][1]

    self.assertNotEqual(first_archive, second_archive)
    self.assertEqual([(10, 360)], second_archive)
