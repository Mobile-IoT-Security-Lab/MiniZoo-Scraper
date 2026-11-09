'use strict';
// Read-only bounded reflection plus redacted WxaPkgWrappingInfo diagnostic text.
// No hook, UI, database write, authentication material, generic object dump,
// or network request.
// Discover actual runtime identities; never seed identity from the search CSV.
const TARGET_APPID = null;
const ATTEMPT_ID = globalThis.PROBE_ATTEMPT_ID || null;
const METADATA_SCHEMA = 'minizoo_public_metadata_v2';

setImmediate(function () {
  if (typeof Java === 'undefined') {
    send({kind: 'metadata-probe-error', error: 'Java bridge missing; use frida CLI with Java bridge'});
    return;
  }
  Java.perform(function () {
    const System = Java.use('java.lang.System');
    const Modifier = Java.use('java.lang.reflect.Modifier');
    const View = Java.use('android.view.View');
    const Activity = Java.use('android.app.Activity');
    const Collection = Java.use('java.util.Collection');
    const Deque = Java.use('java.util.Deque');
    const MapType = Java.use('java.util.Map');
    const isMetadataClass = n => /(?:AppBrandRuntime|AppBrandInitConfig|AppBrandSysConfig|WxaPkgWrappingInfo|WxaVersion[^.]*Info|ModulePkgInfo)/.test(n)
      || n.indexOf('com.tencent.mm.plugin.appbrand.appcache.') === 0;
    const metadataBases = ['com.tencent.mm.plugin.appbrand.AppBrandRuntime',
      'com.tencent.mm.plugin.appbrand.config.AppBrandInitConfig',
      'com.tencent.luggage.sdk.config.AppBrandSysConfigLU',
      'com.tencent.mm.plugin.appbrand.appcache.WxaPkgWrappingInfo'];
    const metadataTypes = [];
    metadataBases.forEach(n => { try { metadataTypes.push(Java.use(n).class); } catch (_) {} });
    function metadataObject(value) {
      const cls = value.getClass();
      return isMetadataClass(String(cls.getName())) || metadataTypes.some(base => base.isAssignableFrom(cls));
    }
    const primitiveTypes = new Set(['boolean', 'byte', 'short', 'int', 'long', 'float', 'double', 'char']);
    const approvedField = /app.?id|pkg|package|version|md5|checksum|sha256|code.?path|running|started|foreground/i;
    let count = 0;
    const focusedAppIds = new Set();

    function inspect(instance) {
      const seen = new Set();
      const schema = [];
      const values = [];
      const objects = [];
      const references = [];
      const lifecycle = [];
      const collections = [];
      const moduleRecords = [];
      const scanLimits = [];
      let targetFound = false;
      let fieldsSeen = 0;

      function visit(object, path, depth, packageScope) {
        if (object === null) return;
        if (depth > 4 || seen.size >= 256) {
          scanLimits.push({path: path, reason: depth > 4 ? 'depth_limit' : 'object_limit'});
          return;
        }
        const identity = String(System.identityHashCode(object));
        references.push({path: path, class_name: String(object.getClass().getName()),
                         identity_hash: identity});
        if (seen.has(identity)) return;
        seen.add(identity);
        let cls = object.getClass();
        const actualClass = String(cls.getName());
        objects.push({path: path, class_name: actualClass, identity_hash: identity});
        packageScope = packageScope || /WxaPkgWrappingInfo|WxaVersion[^.]*Info|ModulePkgInfo/.test(actualClass);
        if (/(?:WxaPkgWrappingInfo|ModulePkgInfo)$/.test(actualClass)) {
          const module = {path: path, class_name: actualClass, fields: {}, provenance: {}};
          ['name', 'md5', 'pkgPath', 'pkgVersion'].forEach(function (fieldName) {
            let declaring = object.getClass();
            for (let level = 0; declaring !== null && level < 12; level++, declaring = declaring.getSuperclass()) {
              try {
                const field = declaring.getDeclaredField(fieldName);
                field.setAccessible(true);
                const value = field.get(object);
                if (value === null) { module.fields[fieldName] = null; break; }
                const text = String(value);
                if (fieldName === 'name' && /^[\w./\-\u0080-\uffff]{0,256}$/.test(text)
                    || fieldName === 'md5' && /^[0-9a-f]{32}$/i.test(text)
                    || fieldName === 'pkgVersion' && /^\d+$/.test(text)
                    || fieldName === 'pkgPath' && (text === '' || /^\/data\/(?:data|user\/\d+)\/com\.tencent\.mm\/.*\.wxapkg$/.test(text))) {
                  module.fields[fieldName] = text;
                  module.provenance[fieldName] = String(declaring.getName()) + '.' + fieldName;
                }
                break;
              } catch (_) {}
            }
          });
          moduleRecords.push(module);
        }
        if (/WxaPkgWrappingInfo/.test(actualClass)) {
          // Known package object's diagnostic string only; redact everything
          // except named version/path/hash fields, never publish its raw text.
          try {
            const summary = String(object.toString());
            const pattern = /(pkgVersion|version|pkgPath|md5|pkgMd5|checksum)\s*[=:]\s*['"]?([^,'"}\s]+)/g;
            let match;
            while ((match = pattern.exec(summary)) !== null) {
              if (/^\d+$|^[0-9a-f]{32}$|^[0-9a-f]{64}$|^\/data\/.*\.wxapkg$/i.test(match[2]))
                values.push({path: path + '.toString().' + match[1], declaring_class: actualClass,
                             type: 'package_diagnostic', value: match[2]});
            }
          } catch (_) {}
        }
        for (let level = 0; cls !== null && level < 12; level++, cls = cls.getSuperclass()) {
          const owner = String(cls.getName());
          // Selected runtime/config objects have obfuscated intermediate
          // superclasses outside com.tencent; follow their actual ancestry.
          if (/^(java\.|android\.|kotlin\.)/.test(owner)) break;
          const fields = cls.getDeclaredFields();
          if (fieldsSeen >= 4000) scanLimits.push({path: path, reason: 'field_limit'});
          for (let i = 0; i < fields.length && fieldsSeen < 4000; i++, fieldsSeen++) {
            const field = fields[i];
            const name = String(field.getName());
            const type = String(field.getType().getName());
            if (Modifier.isStatic(field.getModifiers())) continue;
            const fieldPath = path + '.' + name;
            const fieldSchema = {path: fieldPath, declaring_class: owner, type: type};
            schema.push(fieldSchema);
            try {
              field.setAccessible(true);
              const value = field.get(object);
              if (value === null) continue;
              if (type === 'java.lang.String') {
                const text = String(Java.cast(value, Java.use('java.lang.String')));
                if (/^wx[0-9a-f]{16}$/.test(text)) {
                  targetFound = true;
                  values.push({path: fieldPath, declaring_class: owner, type: type, value: text});
                } else if (/^(?:appName|nickName|nickname|userName|username|developer|developerName|description|signature|iconUrl|appIconUrl)$/.test(name)
                    && /AppBrand(?:InitConfig|SysConfig)/.test(owner)
                    && text.length <= 4096 && !/[\x00]/.test(text)) {
                  values.push({path: fieldPath, declaring_class: owner, type: type, value: text});
                } else if ((approvedField.test(name) || packageScope) && (
                  /^\/data\/(?:data|user\/\d+)\/com\.tencent\.mm\/.*\.wxapkg$/.test(text)
                  || /^[0-9a-f]{32}$|^[0-9a-f]{64}$/i.test(text)
                  || /version/i.test(name) && /^\d+(?:\.\d+){0,3}$/.test(text))) {
                  values.push({path: fieldPath, declaring_class: owner, type: type, value: text});
                }
              } else if (primitiveTypes.has(type) && (approvedField.test(name) || packageScope)) {
                values.push({path: fieldPath, declaring_class: owner, type: type, value: String(value)});
              } else {
                const actual = String(value.getClass().getName());
                fieldSchema.actual_type = actual;
                if (depth === 0 && View.class.isAssignableFrom(value.getClass())) {
                  const view = Java.cast(value, View);
                  lifecycle.push({path: fieldPath, source: 'android.view.View',
                                  shown: view.isShown(), window_focus: view.hasWindowFocus()});
                }
                if (depth === 0 && Activity.class.isAssignableFrom(value.getClass())) {
                  const activity = Java.cast(value, Activity);
                  lifecycle.push({path: fieldPath, source: 'android.app.Activity',
                                  finishing: activity.isFinishing(), destroyed: activity.isDestroyed(),
                                  window_focus: activity.hasWindowFocus()});
                }
                if (metadataObject(value) || depth === 0 && (
                    ['q', 'm'].includes(name) && owner === 'com.tencent.mm.plugin.appbrand.AppBrandRuntime'
                    || ['nj5.x0', 'nj5.i1', 'nj5.z0'].includes(actual))) {
                  visit(value, fieldPath, depth + 1, packageScope);
                } else if ((packageScope ||
                    owner === 'com.tencent.mm.plugin.appbrand.AppBrandRuntime' && fieldPath === 'runtime.E')
                    && (Collection.class.isAssignableFrom(value.getClass())
                           || MapType.class.isAssignableFrom(value.getClass()))) {
                  // APK 8.0.72: B0 -> b -> n0 looks up SysConfig in E using
                  // descendingIterator. Read only existing entries; n0 itself
                  // may instantiate/cache a missing object, so never call it.
                  const runtimeRegistry = fieldPath === 'runtime.E';
                  const collection = Collection.class.isAssignableFrom(value.getClass())
                    ? Java.cast(value, Collection) : Java.cast(value, MapType).values();
                  const iterator = runtimeRegistry
                    ? Java.cast(value, Deque).descendingIterator() : collection.iterator();
                  let inspected = 0, metadataItems = 0;
                  let selectedConfig = null;
                  const itemTypes = new Set();
                  while (iterator.hasNext() && inspected < 128) {
                    const item = iterator.next();
                    if (item !== null) itemTypes.add(String(item.getClass().getName()));
                    if (item !== null && metadataObject(item)) {
                      const itemPath = fieldPath + '[' + inspected + ']';
                      const isSysConfig = /AppBrandSysConfig/.test(String(item.getClass().getName()));
                      // The getter returns the first matching SysConfig in
                      // this order; older entries cannot supply the binding.
                      if (!runtimeRegistry || !isSysConfig || selectedConfig === null) {
                        metadataItems++;
                        visit(item, itemPath, depth + 1, packageScope);
                        if (runtimeRegistry && isSysConfig) selectedConfig = itemPath;
                      }
                    }
                    inspected++;
                  }
                  collections.push({path: fieldPath, size: collection.size(), inspected: inspected,
                                    metadata_items: metadataItems, item_types: Array.from(itemTypes),
                                    declaring_class: owner, field_type: type,
                                    lookup_order: runtimeRegistry ? 'descending' : 'iteration',
                                    selected_config_path: selectedConfig,
                                    truncated: iterator.hasNext()});
                }
              }
            } catch (error) {
              fieldSchema.read_error = String(error).slice(0, 160);
            }
          }
        }
      }

      visit(instance, 'runtime', 0);
      const focused = lifecycle.some(item => item.window_focus === true && (
        item.shown === true || item.source === 'android.app.Activity'
        && item.finishing === false && item.destroyed === false));
      if (focused) values.forEach(value => {
        if (value.declaring_class === 'com.tencent.mm.plugin.appbrand.AppBrandRuntime'
            && value.path.split('.').length === 2 && /^wx[0-9a-f]{16}$/.test(value.value))
          focusedAppIds.add(value.value);
      });
      if (fieldsSeen >= 4000) scanLimits.push({path: 'runtime', reason: 'field_limit'});
      send({kind: 'runtime-package-metadata', target_appid: TARGET_APPID,
            attempt_id: ATTEMPT_ID, metadata_schema: METADATA_SCHEMA,
            target_found: targetFound, runtime_class: String(instance.getClass().getName()),
            process_id: Process.id, observed_at: new Date().toISOString(),
            schema: schema, objects: objects, references: targetFound ? references : [],
            lifecycle: targetFound ? lifecycle : [],
            collections: targetFound ? collections : [], values: targetFound ? values : [],
            modules: targetFound ? moduleRecords : [], scan_limits: scanLimits,
            note: 'Field provenance only; inspect package path and release semantics before certifying.'});
    }

    function readNamedField(instance, name, limit) {
      limit = limit || 4096;
      let cls = instance.getClass();
      for (let level = 0; cls !== null && level < 12; level++, cls = cls.getSuperclass()) {
        try {
          const field = cls.getDeclaredField(name);
          field.setAccessible(true);
          const value = field.get(instance);
          if (value === null) return null;
          const text = String(value);
          if (text.length <= limit && !/[\x00]/.test(text))
            return {value: text, declaring_class: String(cls.getName()), field: name};
          return null;
        } catch (_) {}
      }
      return null;
    }

    function readOpenedPublicMetadata() {
      // Read only already-loaded public app records keyed by the observed
      // foreground AppID. No query hint, private-message DB or network request.
      if (focusedAppIds.size !== 1) {
        send({kind: 'metadata-probe-complete', runtime_instances: count});
        return;
      }
      let inspected = 0;
      const className = 'com.tencent.mm.plugin.appbrand.config.WxaAttributes';
      try {
        Java.choose(className, {
          onMatch: function (instance) {
            if (inspected++ >= 128) return 'stop';
            const identity = readNamedField(instance, 'field_appId');
            if (!identity || !focusedAppIds.has(identity.value)) return;
            const fields = {}, provenance = {};
            const allowed = {
              field_nickname: 'name', field_username: 'username',
              field_signature: 'description', field_brandIconURL: 'icon_url',
              field_dynamicInfo: 'raw_dynamic_info', field_registerSource: 'raw_register_source',
              field_appInfo: 'raw_app_info'
            };
            Object.keys(allowed).forEach(name => {
              const entry = readNamedField(instance, name, /dynamicInfo|registerSource|appInfo/.test(name) ? 65536 : 4096);
              if (entry && entry.value) {
                fields[allowed[name]] = entry.value;
                provenance[allowed[name]] = entry;
              }
            });
            send({kind: 'runtime-public-metadata', attempt_id: ATTEMPT_ID,
                  appid: identity.value, metadata_class: className, metadata_schema: METADATA_SCHEMA,
                  process_id: Process.id, observed_at: new Date().toISOString(),
                  identity_field: identity, fields: fields, provenance: provenance});
          },
          onComplete: function () {
            send({kind: 'metadata-public-records-complete', inspected: inspected,
                  truncated: inspected > 128});
            readCachedPublicMetadata();
          }
        });
      } catch (_) {
        // Optional public fields remain empty on unsupported WeChat builds.
        send({kind: 'metadata-public-records-unavailable', metadata_class: className});
        readCachedPublicMetadata();
      }
    }

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
                     metadata_schema: METADATA_SCHEMA};
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

    const choices = ['com.tencent.mm.plugin.appbrand.AppBrandRuntime',
                     'com.tencent.mm.plugin.appbrand.config.AppBrandSysConfigWC',
                     'com.tencent.mm.plugin.appbrand.config.AppBrandInitConfigWC'];
    function chooseNext(index) {
      if (index >= choices.length) {
        readOpenedPublicMetadata();
        return;
      }
      let localCount = 0;
      try {
        Java.choose(choices[index], {
          onMatch: function (instance) {
            if (localCount++ >= 6) return 'stop';
            count++;
            inspect(instance);
          },
          onComplete: function () {
            send({kind: 'metadata-class-complete', class_name: choices[index], instances: localCount});
            chooseNext(index + 1);
          }
        });
      } catch (error) {
        send({kind: 'metadata-probe-error', class_name: choices[index], error: String(error)});
        chooseNext(index + 1);
      }
    }
    chooseNext(0);
  });
});
