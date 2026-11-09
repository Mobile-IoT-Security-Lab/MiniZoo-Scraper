'use strict';
// Supplemental public metadata only. Its AppID comes from a verified focused
// runtime, not a requested search name/AppID. No app launch, hook or DB write.
const CONTEXT = globalThis.CACHED_METADATA_CONTEXT;
const METADATA_SCHEMA = 'minizoo_public_metadata_v2';
if (!CONTEXT || !/^wx[0-9a-f]{16}$/.test(CONTEXT.appid || '')
    || !CONTEXT.attempt_id || !Number.isInteger(CONTEXT.runtime_process_id))
  throw new Error('Invalid runtime metadata context');
const ATTEMPT_ID = CONTEXT.attempt_id;
const focusedAppIds = new Set([CONTEXT.appid]);
let count = 0;
setImmediate(function () {
  Java.perform(function () {
    const processName = String(Java.use('android.app.ActivityThread').currentProcessName());
    if (processName !== 'com.tencent.mm') throw new Error('Expected existing WeChat main process');
    function readCachedPublicMetadata() {
      // Only already-open public attribute databases. The runtime, never the
      // search query, supplies the single AppID. No DB is opened or modified.
      const appid = Array.from(focusedAppIds)[0];
      if (!/^wx[0-9a-f]{16}$/.test(appid || '')) {
        send({kind: 'metadata-probe-complete', runtime_instances: count});
        return;
      }
      const seen = new Set();
      let inspected = 0;
      const bound = {attempt_id: ATTEMPT_ID, appid: appid, process_id: Process.id,
                     metadata_schema: METADATA_SCHEMA, metadata_source: 'wechat_main_cache',
                     runtime_process_id: CONTEXT.runtime_process_id, metadata_process_name: processName};
      try {
        Java.choose('com.tencent.wcdb.database.SQLiteDatabase', {
          onMatch: function (db) {
            if (inspected++ >= 128) return 'stop';
            let path;
            try { path = String(db.getPath()); } catch (_) { return; }
            if (!/^\/data\/(?:data|user\/\d+)\/com\.tencent\.mm\/MicroMsg\/[0-9a-f]{32}\/AppBrandComm\.db$/.test(path)
                || seen.has(path) || seen.size >= 4) return;
            seen.add(path);
            for (const table of ['WxaAttributesTable', 'WxaAttrAvailableBackupTable']) {
              let cursor = null, count = 0, truncated = false;
              try {
                const fields = ['appId', 'nickname', 'signature', 'brandIconURL', 'registerSource', 'dynamicInfo', 'appInfo'];
                // AppID was regex-validated above; identifiers are fixed constants.
                const sql = 'SELECT ' + fields.join(',') + ' FROM ' + table +
                            " WHERE appId='" + appid + "' LIMIT 3";
                cursor = db.rawQuery.overload('java.lang.String', '[Ljava.lang.Object;').call(db, sql, null);
                while (cursor.moveToNext()) {
                  if (count >= 2) { truncated = true; break; }
                  const row = {}, invalid = [];
                  fields.forEach((key, index) => {
                    let value = cursor.isNull(index) ? null : String(cursor.getString(index));
                    const limit = /dynamicInfo|registerSource|appInfo/.test(key) ? 65536 : 4096;
                    if (value !== null && (value.length > limit || /[\x00]/.test(value))) {
                      invalid.push(key); value = null;
                    }
                    row[key] = value;
                  });
                  count++;
                  send(Object.assign({}, bound, {kind: 'runtime-cached-public-metadata',
                    database_path: path, table: table, row: row, invalid_fields: invalid,
                    observed_at: new Date().toISOString()}));
                }
                send(Object.assign({}, bound, {kind: 'metadata-public-table-complete',
                  database_path: path, table: table, count: count, truncated: truncated}));
              } catch (_) {
                send(Object.assign({}, bound, {kind: 'metadata-public-table-unavailable',
                  database_path: path, table: table}));
              } finally { if (cursor !== null) cursor.close(); }
            }
          },
          onComplete: function () {
            send(Object.assign({}, bound, {kind: 'metadata-public-cache-complete',
              databases: seen.size, inspected: inspected, truncated: inspected > 128}));
            send({kind: 'metadata-probe-complete', runtime_instances: count});
          }
        });
      } catch (_) {
        send(Object.assign({}, bound, {kind: 'metadata-public-cache-unavailable'}));
        send({kind: 'metadata-probe-complete', runtime_instances: count});
      }
    }

    readCachedPublicMetadata();
  });
});
