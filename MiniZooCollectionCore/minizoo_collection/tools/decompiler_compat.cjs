// Compatibility for the pinned wxappUnpacker; original vendor files stay intact.
// Parse modern WXML factories and CSS calls as JavaScript, not delimiter slices.
const fs = require('fs');
const path = require('path');
const Module = require('module');
const toolRoot = process.env.WXAPP_TOOL_ROOT;

function dependencies() {
  if (!toolRoot) throw new Error('WXAPP_TOOL_ROOT is required for compatibility parsing');
  return {
    esprima: require(path.join(toolRoot, 'node_modules/esprima')),
    cheerio: require(path.join(toolRoot, 'node_modules/cheerio')),
  };
}
function walk(node, visit) {
  if (!node || typeof node !== 'object') return;
  if (visit(node) === false) return;
  for (const [key, child] of Object.entries(node)) {
    if (key === 'range') continue;
    if (Array.isArray(child)) child.forEach(value => walk(value, visit));
    else if (child && typeof child === 'object') walk(child, visit);
  }
}
function scripts(content) {
  if (!/^\s*</.test(content)) return [content];
  const $ = dependencies().cheerio.load(content);
  const result = [];
  $('script').each((_, element) => result.push($(element).html() || ''));
  return result;
}
function declarator(statement, name) {
  return statement.type === 'VariableDeclaration'
    ? statement.declarations.find(item => item.id.type === 'Identifier' && item.id.name === name)
    : null;
}
function pathGuard(statement) {
  const test = statement.type === 'IfStatement' && statement.test;
  return test && test.type === 'LogicalExpression' && test.operator === '&&'
    && test.left.type === 'Identifier' && test.left.name === 'path'
    && test.right.type === 'MemberExpression' && test.right.object.name === 'e_';
}
function wxmlFrames(content) {
  const {esprima} = dependencies();
  const frames = [];
  let sharedRequire = '{}';
  for (const code of scripts(content)) {
    const ast = esprima.parseScript(code, {range: true});
    walk(ast, node => {
      const target = node.type === 'AssignmentExpression' ? node.left
        : node.type === 'VariableDeclarator' ? node.id : null;
      const value = node.type === 'AssignmentExpression' ? node.right
        : node.type === 'VariableDeclarator' ? node.init : null;
      if (!target || target.type !== 'Identifier' || !/^\$gwx(?:_\w+)*$/.test(target.name)) return;
      if (!value || value.type !== 'CallExpression' || value.callee.type !== 'FunctionExpression') {
        throw new Error('Unsupported WXML factory: ' + target.name);
      }
      const returned = value.callee.body.body.find(item => item.type === 'ReturnStatement'
        && item.argument && item.argument.type === 'FunctionExpression');
      if (!returned) throw new Error('WXML factory has no registration function: ' + target.name);
      const body = returned.argument.body;
      const xIndex = body.body.findIndex(item => declarator(item, 'x'));
      const endIndex = body.body.findIndex(pathGuard);
      if (xIndex < 0 || endIndex <= xIndex) throw new Error('WXML registration boundaries unavailable: ' + target.name);
      const requireStatement = body.body.find(item => declarator(item, 'nv_require'));
      if (requireStatement) {
        const init = declarator(requireStatement, 'nv_require').init;
        if (!init || init.type !== 'CallExpression' || init.callee.type !== 'FunctionExpression') {
          throw new Error('Unsupported WXS module initializer');
        }
        const mapStatement = init.callee.body.body.find(item => declarator(item, 'nnm'));
        const map = mapStatement && declarator(mapStatement, 'nnm').init;
        if (!map || map.type !== 'ObjectExpression') throw new Error('WXS module map unavailable');
        sharedRequire = code.slice(...map.range);
      }
      frames.push({
        name: target.name,
        opsCode: code.slice(body.range[0] + 1, body.body[xIndex].range[0]),
        registrationCode: code.slice(body.body[xIndex].range[0], body.body[endIndex].range[0]),
        requireInfo: sharedRequire,
      });
    });
  }
  if (!frames.length) throw new Error('No supported WXML factories found');
  return frames;
}
function cssScript(content) {
  const {esprima} = dependencies();
  const calls = [];
  const isCss = node => node && node.type === 'CallExpression'
    && node.callee.type === 'Identifier' && node.callee.name === 'setCssToHead';
  for (const code of scripts(content)) {
    const ast = esprima.parseScript(code, {range: true});
    walk(ast, node => {
      // HTML page styles invoke the function returned by setCssToHead.
      if (node.type === 'CallExpression' && isCss(node.callee)) {
        calls.push(code.slice(...node.range) + ';');
        return false;
      }
      if (node.type === 'AssignmentExpression' && isCss(node.right)
          && node.left.type === 'MemberExpression' && node.left.object.name === '__wxAppCode__') {
        calls.push(code.slice(...node.range) + ';');
        return false;
      }
    });
  }
  return calls.join('\n');
}
function expected(kind, filename) {
  const audit = process.env.WXAPP_COMPAT_AUDIT;
  if (!audit) throw new Error('WXAPP_COMPAT_AUDIT is required for reconstruction auditing');
  fs.appendFileSync(audit, JSON.stringify({kind, path: path.resolve(filename)}) + '\n');
}
function styleOutput(saveDir, dir, id) {
  if (/^\d+$/.test(id)) return path.resolve(dir, '__wuBaseWxss__', id + '.wxss');
  const logical = id.replace(/^\.\//, '').replace(/^\//, '');
  if (!logical.endsWith('.wxss') || logical.includes('\\') || logical.includes('\0')
      || logical.split('/').some(part => !part || part === '..' || part === '.')) {
    throw new Error('Unsafe common stylesheet path: ' + id);
  }
  return path.resolve(saveDir, logical);
}

// These references intentionally resolve in the vendor module's lexical scope:
// its existing expression/WXS/WXML reconstruction stays unchanged.
const modernFrame = `
const legacyDoFrame = doFrame;
module.exports.doFrame = function(name, cb, order, mainDir) {
  if (!fs.readFileSync(name, 'utf8').includes('$gwx_XC_')) return legacyDoFrame(name, cb, order, mainDir);
  const compat = require(${JSON.stringify(__filename)});
  wu.get(name, content => {
    for (const frame of compat.wxmlFrames(content)) {
      const restore = z => {
        let rD = {}, rE = {}, rF = {}, requireInfo = {}, x;
        const vm = createVM({d_: rD, e_: rE, f_: rF,
          _vmRev_(data) { [x, requireInfo] = data; },
          nv_require(modulePath) { return () => modulePath; }
        });
        vm.run(frame.registrationCode + '\\n_vmRev_([x,' + frame.requireInfo + ']);');
        const dir = mainDir || path.dirname(name), pF = {}, wxsList = {};
        for (const info in rF) if (typeof rF[info] === 'function') {
          const target = path.resolve(dir, (info[0] === '/' ? '.' : '') + info), ref = rF[info]();
          pF[ref] = info;
          if (!requireInfo[ref]) throw new Error('Missing WXS module: ' + ref);
          wu.save(target, doWxs(requireInfo[ref].toString()));
        }
        for (const info in rF) if (typeof rF[info] === 'object') {
          const target = path.resolve(dir, (info[0] === '/' ? '.' : '') + info), parts = [];
          for (const dependency in rF[info]) {
            const ref = rF[info][dependency]();
            if (ref.includes(':')) {
              if (!requireInfo[ref]) throw new Error('Missing inline WXS module: ' + ref);
              parts.push('<wxs module="' + dependency + '">\\n' + doWxs(requireInfo[ref].toString()) + '\\n</wxs>');
            } else parts.push('<wxs module="' + dependency + '" src="' + wu.toDir(pF[ref] || ref.slice(2), info) + '" />');
          }
          wxsList[target] = parts.join('\\n');
        }
        for (const entry in rE) tryWxml(dir, entry, rE[entry].f.toString(), z, x, rD[entry], wxsList, order.includes('m'));
      };
      if (/function gz\\$gwx|\\(function\\(z\\)/.test(frame.opsCode)) getZ(frame.opsCode, restore);
      else restore([]);
    }
    cb({[name]: 4});
  });
};
`;

function replaceOnce(source, before, after, name) {
  if (source.split(before).length !== 2) throw new Error('Pinned compatibility target changed: ' + name);
  return source.replace(before, after);
}
function patchSource(filename, source) {
  if (filename === 'wuWxml.js') {
    source = replaceOnce(source, 'function tryWxml(dir, name, code, z, xPool, rDs, ...args) {',
      'function tryWxml(dir, name, code, z, xPool, rDs, ...args) {\nrequire(' + JSON.stringify(__filename) + ').expected("wxml", path.resolve(dir, name));', 'WXML output inventory');
    return source + modernFrame;
  }
  if (filename === 'wuRestoreZ.js') {
    if (source.split('\\d*\\_\\d+').length !== 4) throw new Error('Pinned WXML operation patterns changed');
    source = source.replaceAll('\\d*\\_\\d+', '[A-Za-z0-9_]+');
    return source;
  }
  if (filename === 'wuJs.js') {
    return replaceOnce(source, 'define(name, func) {', 'define(name, func) {\nrequire(' + JSON.stringify(__filename) + ').expected("js", path.resolve(dir, name));', 'JavaScript module inventory');
  }
  if (filename === 'wuWxss.js') {
    // Modern common stylesheet tables use original filenames instead of integer
    // indexes. Keep those keys intact and restore their original import paths.
    source = replaceOnce(source, 'if (typeof data === "number") return addStat(data);',
      'if (typeof data === "number" || typeof data === "string") return addStat(data);\n'
      + '            if (!Array.isArray(data)) throw new Error("Missing common stylesheet data");', 'Named CSS import statistics');
    source = replaceOnce(source, 'var isPure = typeof data === "number";',
      'var isPure = typeof data === "number" || typeof data === "string";', 'Named CSS imports');
    source = replaceOnce(source, 'let exactData = isPure ? pureData[data] : data;',
      'let exactData = isPure ? pureData[data] : data;\n'
      + '            if (!Array.isArray(exactData)) throw new Error("Missing common stylesheet: " + data);', 'Missing CSS import detection');
    source = replaceOnce(source, 'let newFile = path.resolve(dir, "__wuBaseWxss__/" + id + ".wxss");',
      'let newFile = require(' + JSON.stringify(__filename) + ').styleOutput(saveDir, dir, id);', 'Named CSS output path');
    source = replaceOnce(source, 'id = Number.parseInt(id);',
      '// Keep both numeric and named keys unchanged.', 'Named CSS key preservation');
    const before = 'code = code.slice(0, code.indexOf("\\n"));';
    source = replaceOnce(source, before, 'code = require(' + JSON.stringify(__filename) + ').cssScript(code);', 'HTML page CSS');
    source = replaceOnce(source, 'runList[lastName] = code.slice(code.indexOf("setCssToHead("));', 'runList[lastName] = code;', 'CSS invocation boundary');
    source = replaceOnce(source, 'cssFile = this.cssFile;', 'cssFile = this.cssFile;\nrequire(' + JSON.stringify(__filename) + ').expected("wxss", wu.changeExt(cssFile, ".wxss"));', 'CSS output inventory');
    const start = source.indexOf("            scriptCode = scriptCode.slice(scriptCode.lastIndexOf('window.__wcc_version__'));");
    const end = source.indexOf('            //remove var __wxAppCode__ = {};', start);
    if (start < 0 || end < 0) throw new Error('Pinned CSS frame initializer changed');
    const legacy = source.slice(start, end).replace('let mainCode =', 'mainCode =');
    const modern = '            let mainCode;\n'
      + '            if (!scriptCode.includes("window.__wcc_version__")) {\n'
      + '                mainCode = require(' + JSON.stringify(__filename) + ').cssScript(code);\n'
      + '                if (!mainCode) throw new Error("No CSS registrations in modern page frame");\n'
      + '            } else {\n' + legacy + '            }\n';
    source = source.slice(0, start) + modern + source.slice(end);
    return source;
  }
  return source;
}
if (toolRoot) {
  const originalLoad = Module._extensions['.js'];
  Module._extensions['.js'] = function(module, filename) {
    const basename = path.basename(filename);
    if (path.dirname(filename) === path.resolve(toolRoot)
        && ['wuWxml.js', 'wuRestoreZ.js', 'wuWxss.js', 'wuJs.js'].includes(basename)) {
      return module._compile(patchSource(basename, fs.readFileSync(filename, 'utf8')), filename);
    }
    return originalLoad(module, filename);
  };
}
module.exports = {wxmlFrames, cssScript, patchSource, expected, styleOutput};
