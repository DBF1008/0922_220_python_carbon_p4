import os
import shutil
import tempfile
from unittest import TestCase

from mock import MagicMock, patch

import carbon.conf

# carbon.storage computes its config file paths from CONF_DIR at import time,
# so make sure one is set before carbon.writer (and thus carbon.storage) is
# imported for the first time.
_TEST_CONF_DIR = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), 'data', 'conf-directory')
carbon.conf.settings.setdefault('CONF_DIR', _TEST_CONF_DIR)

import carbon.cache  # noqa: E402
import carbon.storage  # noqa: E402
from carbon import state, writer  # noqa: E402
from carbon.cache import MetricCache  # noqa: E402
from carbon.storage import (  # noqa: E402
    Archive, DefaultSchema, PatternSchema, defaultAggregation, defaultSchema)


class SchemaMatcherTest(TestCase):

  def makeSchemas(self):
    return [
        PatternSchema('carbon', r'^carbon\.', [Archive.fromString('60:90d')]),
        PatternSchema('everything', r'.*', [Archive.fromString('60s:1d')]),
    ]

  def test_first_match_wins(self):
    schemas = self.makeSchemas()
    matcher = writer.SchemaMatcher(schemas)
    self.assertIs(matcher.match('carbon.foo'), schemas[0])
    self.assertIs(matcher.match('foo.bar'), schemas[1])

  def test_match_result_is_cached(self):
    schemas = self.makeSchemas()
    matcher = writer.SchemaMatcher(schemas)
    with patch.object(schemas[0], 'matches',
                      wraps=schemas[0].matches) as matches:
      self.assertIs(matcher.match('carbon.foo'), schemas[0])
      self.assertIs(matcher.match('carbon.foo'), schemas[0])
    self.assertEqual(matches.call_count, 1)
    self.assertIs(matcher._cache['carbon.foo'], schemas[0])

  def test_distinct_metrics_cached_separately(self):
    schemas = self.makeSchemas()
    matcher = writer.SchemaMatcher(schemas)
    matcher.match('carbon.foo')
    matcher.match('foo.bar')
    self.assertEqual(len(matcher._cache), 2)

  def test_default_schema_fallback(self):
    matcher = writer.SchemaMatcher([defaultSchema])
    self.assertIs(matcher.match('anything.at.all'), defaultSchema)

  def test_no_match_returns_none(self):
    matcher = writer.SchemaMatcher([])
    self.assertIsNone(matcher.match('anything.at.all'))

  def test_cache_cleared_when_full(self):
    schemas = self.makeSchemas()
    matcher = writer.SchemaMatcher(schemas, max_cache_size=2)
    matcher.match('metric.a')
    matcher.match('metric.b')
    self.assertEqual(len(matcher._cache), 2)
    matcher.match('metric.c')
    self.assertEqual(len(matcher._cache), 1)
    self.assertIs(matcher._cache['metric.c'], schemas[1])


class WriterSchemaReloadTest(TestCase):

  storage_conf = """\
[carbon]
pattern = ^carbon\\.
retentions = 60:90d

[default_1min_for_1day]
pattern = .*
retentions = 60s:1d
"""

  aggregation_conf = """\
[min]
pattern = \\.min$
xFilesFactor = 0.1
aggregationMethod = min

[default_average]
pattern = .*
xFilesFactor = 0.5
aggregationMethod = average
"""

  def setUp(self):
    self.conf_dir = tempfile.mkdtemp(prefix='carbon-writer-test-')
    self.storage_conf_path = os.path.join(self.conf_dir, 'storage-schemas.conf')
    self.aggregation_conf_path = os.path.join(
        self.conf_dir, 'storage-aggregation.conf')
    self._write(self.storage_conf_path, self.storage_conf)
    self._write(self.aggregation_conf_path, self.aggregation_conf)

    self._saved_globals = (
        writer.SCHEMAS,
        writer.SCHEMA_MATCHER,
        writer.AGGREGATION_SCHEMAS,
        writer.AGGREGATION_SCHEMA_MATCHER,
    )
    self._patches = [
        patch.object(carbon.storage, 'STORAGE_SCHEMAS_CONFIG',
                     self.storage_conf_path),
        patch.object(carbon.storage, 'STORAGE_AGGREGATION_CONFIG',
                     self.aggregation_conf_path),
        patch.object(state, 'database', self._makeDatabase()),
    ]
    for p in self._patches:
      p.start()
    writer.reloadStorageSchemas()
    writer.reloadAggregationSchemas()

  def tearDown(self):
    (writer.SCHEMAS, writer.SCHEMA_MATCHER,
     writer.AGGREGATION_SCHEMAS, writer.AGGREGATION_SCHEMA_MATCHER
     ) = self._saved_globals
    for p in reversed(self._patches):
      p.stop()
    shutil.rmtree(self.conf_dir, ignore_errors=True)

  def _makeDatabase(self):
    database = MagicMock()
    database.aggregationMethods = ['average', 'sum', 'last', 'max', 'min']
    return database

  def _write(self, path, content):
    with open(path, 'w') as f:
      f.write(content)

  def test_reload_loads_schemas_into_matcher(self):
    schema = writer.SCHEMA_MATCHER.match('carbon.foo')
    self.assertEqual(schema.name, 'carbon')
    self.assertEqual([a.getTuple() for a in schema.archives], [(60, 129600)])
    aggregation_schema = writer.AGGREGATION_SCHEMA_MATCHER.match('foo.min')
    self.assertEqual(aggregation_schema.name, 'min')
    self.assertEqual(aggregation_schema.archives, (0.1, 'min'))

  def test_reload_storage_schemas_invalidates_cache(self):
    matcher = writer.SCHEMA_MATCHER
    matcher.match('carbon.foo')
    self.assertIn('carbon.foo', matcher._cache)

    self._write(self.storage_conf_path, self.storage_conf.replace(
        'retentions = 60:90d', 'retentions = 10s:6h'))
    writer.reloadStorageSchemas()

    self.assertIsNot(writer.SCHEMA_MATCHER, matcher)
    self.assertEqual(writer.SCHEMA_MATCHER._cache, {})
    self.assertIs(writer.SCHEMA_MATCHER.schemas, writer.SCHEMAS)
    schema = writer.SCHEMA_MATCHER.match('carbon.foo')
    self.assertEqual([a.getTuple() for a in schema.archives], [(10, 2160)])

  def test_reload_aggregation_schemas_invalidates_cache(self):
    matcher = writer.AGGREGATION_SCHEMA_MATCHER
    matcher.match('foo.min')
    self.assertIn('foo.min', matcher._cache)

    self._write(self.aggregation_conf_path, self.aggregation_conf.replace(
        'xFilesFactor = 0.1', 'xFilesFactor = 0.3'))
    writer.reloadAggregationSchemas()

    self.assertIsNot(writer.AGGREGATION_SCHEMA_MATCHER, matcher)
    self.assertEqual(writer.AGGREGATION_SCHEMA_MATCHER._cache, {})
    self.assertIs(writer.AGGREGATION_SCHEMA_MATCHER.schemas,
                  writer.AGGREGATION_SCHEMAS)
    schema = writer.AGGREGATION_SCHEMA_MATCHER.match('foo.min')
    self.assertEqual(schema.archives, (0.3, 'min'))

  def test_failed_storage_reload_keeps_matcher_and_cache(self):
    matcher = writer.SCHEMA_MATCHER
    schemas = writer.SCHEMAS
    matcher.match('carbon.foo')

    with patch.object(writer, 'loadStorageSchemas',
                      side_effect=Exception('boom')):
      writer.reloadStorageSchemas()

    self.assertIs(writer.SCHEMA_MATCHER, matcher)
    self.assertIs(writer.SCHEMAS, schemas)
    self.assertIn('carbon.foo', matcher._cache)

  def test_failed_aggregation_reload_keeps_matcher_and_cache(self):
    matcher = writer.AGGREGATION_SCHEMA_MATCHER
    schemas = writer.AGGREGATION_SCHEMAS
    matcher.match('foo.min')

    with patch.object(writer, 'loadAggregationSchemas',
                      side_effect=Exception('boom')):
      writer.reloadAggregationSchemas()

    self.assertIs(writer.AGGREGATION_SCHEMA_MATCHER, matcher)
    self.assertIs(writer.AGGREGATION_SCHEMAS, schemas)
    self.assertIn('foo.min', matcher._cache)

  def test_default_schema_fallback_after_reload(self):
    self._write(self.storage_conf_path, '')
    writer.reloadStorageSchemas()
    self.assertIs(writer.SCHEMA_MATCHER.match('anything'), defaultSchema)
    self._write(self.aggregation_conf_path, '')
    writer.reloadAggregationSchemas()
    self.assertIs(writer.AGGREGATION_SCHEMA_MATCHER.match('anything'),
                  defaultAggregation)


class WriteCachedDataPointsTest(TestCase):

  def setUp(self):
    self._saved_globals = (
        writer.SCHEMAS,
        writer.SCHEMA_MATCHER,
        writer.AGGREGATION_SCHEMAS,
        writer.AGGREGATION_SCHEMA_MATCHER,
    )
    self.database = MagicMock()
    self.database.aggregationMethods = ['average', 'sum', 'last', 'max', 'min']
    self.database.exists.side_effect = [False, True]
    self._patches = [
        patch.object(state, 'database', self.database),
        patch.dict('carbon.conf.settings',
                   {'LOG_CREATES': False, 'ENABLE_TAGS': False,
                    'CACHE_SIZE_HARD_MAX': float('inf')}),
    ]
    for p in self._patches:
      p.start()
    carbon.cache._Cache = None
    writer.reloadStorageSchemas()
    writer.reloadAggregationSchemas()

  def tearDown(self):
    (writer.SCHEMAS, writer.SCHEMA_MATCHER,
     writer.AGGREGATION_SCHEMAS, writer.AGGREGATION_SCHEMA_MATCHER
     ) = self._saved_globals
    for p in reversed(self._patches):
      p.stop()
    carbon.cache._Cache = None

  def test_create_uses_cached_schema_matches(self):
    cache = MetricCache()
    cache.store('carbon.test.metric', (1234567890, 1.0))

    writer.writeCachedDataPoints()

    # storage-schemas.conf from the test conf directory:
    # [carbon] pattern = ^carbon\. retentions = 60:90d
    self.database.create.assert_called_once_with(
        'carbon.test.metric', [(60, 129600)], 0.5, 'average')
    self.assertIn('carbon.test.metric', writer.SCHEMA_MATCHER._cache)
    self.assertIn('carbon.test.metric',
                  writer.AGGREGATION_SCHEMA_MATCHER._cache)
    schema = writer.SCHEMA_MATCHER._cache['carbon.test.metric']
    self.assertEqual(schema.name, 'carbon')

  def test_no_matching_storage_schema_raises(self):
    writer.SCHEMA_MATCHER = writer.SchemaMatcher([])
    cache = MetricCache()
    cache.store('carbon.test.metric', (1234567890, 1.0))

    with self.assertRaises(Exception):
      writer.writeCachedDataPoints()
    self.assertFalse(self.database.create.called)
