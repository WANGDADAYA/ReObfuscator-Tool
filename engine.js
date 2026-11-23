/* ==========================================================================
 * obfuscator.io 字符串表还原引擎（纯 JS，无依赖）
 * 与 obfuscator_deobfuscate.py 完全同一套算法：
 *   遮罩词法 -> 结构识别脚手架(数组函数/解码器/包装器/别名/IIFE)
 *   -> 在隔离作用域里执行脚手架 -> 用真实参数调用解码器 -> 就地替换
 * ========================================================================== */
(function (root) {
  'use strict';

  var IDENT = '[A-Za-z_$][\\w$]*';
  var KEYWORDS_BEFORE_REGEX = {
    'return': 1, 'typeof': 1, 'case': 1, 'in': 1, 'of': 1, 'new': 1,
    'delete': 1, 'void': 1, 'instanceof': 1, 'do': 1, 'else': 1,
    'yield': 1, 'await': 1
  };

  /* ---------------- 1. 词法遮罩 ---------------- */
  function maskJs(src) {
    var n = src.length;
    var mask = new Array(n);
    for (var z = 0; z < n; z++) mask[z] = ' ';
    var i = 0, lastSig = '', lastWord = '';

    while (i < n) {
      var c = src.charAt(i);

      if (/\s/.test(c)) { i++; continue; }

      if (c === '/' && src.charAt(i + 1) === '/') {
        while (i < n && src.charAt(i) !== '\n') i++;
        continue;
      }
      if (c === '/' && src.charAt(i + 1) === '*') {
        var jj = src.indexOf('*/', i + 2);
        i = jj < 0 ? n : jj + 2;
        continue;
      }

      if (c === '"' || c === "'") {
        var q = c, qpos = i;
        i++;
        while (i < n) {
          if (src.charAt(i) === '\\') { i += 2; continue; }
          if (src.charAt(i) === q) { i++; break; }
          i++;
        }
        mask[qpos] = q;
        mask[Math.min(i - 1, n - 1)] = q;
        lastSig = 'a'; lastWord = '';
        continue;
      }

      if (c === '`') {
        var tpos = i;
        i++;
        while (i < n) {
          if (src.charAt(i) === '\\') { i += 2; continue; }
          if (src.charAt(i) === '`') { i++; break; }
          i++;
        }
        mask[tpos] = '`';
        mask[Math.min(i - 1, n - 1)] = '`';
        lastSig = 'a'; lastWord = '';
        continue;
      }

      if (c === '/') {
        var isRegex = ('=([{,;:!&|?+-*%^~<>}'.indexOf(lastSig) >= 0) ||
          lastSig === '' || KEYWORDS_BEFORE_REGEX[lastWord] === 1;
        if (isRegex) {
          i++;
          var inClass = false;
          while (i < n) {
            var ch = src.charAt(i);
            if (ch === '\\') { i += 2; continue; }
            if (ch === '[') inClass = true;
            else if (ch === ']') inClass = false;
            else if (ch === '/' && !inClass) { i++; break; }
            else if (ch === '\n') break;
            i++;
          }
          while (i < n && /[a-zA-Z]/.test(src.charAt(i))) i++;
          lastSig = '/'; lastWord = '';
          continue;
        }
      }

      if (/[A-Za-z_$]/.test(c)) {
        var k = i;
        while (k < n && /[A-Za-z0-9_$]/.test(src.charAt(k))) k++;
        for (var t = i; t < k; t++) mask[t] = src.charAt(t);
        lastWord = src.slice(i, k);
        lastSig = 'a';
        i = k;
        continue;
      }

      mask[i] = c;
      lastSig = c; lastWord = '';
      i++;
    }
    return mask.join('');
  }

  function skipWs(m, i) {
    while (i < m.length && /\s/.test(m.charAt(i))) i++;
    return i;
  }

  function matchPair(m, i) {
    var pairs = { '(': ')', '[': ']', '{': '}' };
    var open = m.charAt(i), close = pairs[open];
    if (!close) return -1;
    var depth = 0;
    while (i < m.length) {
      var ch = m.charAt(i);
      if (ch === open) depth++;
      else if (ch === close) { depth--; if (depth === 0) return i; }
      i++;
    }
    return -1;
  }

  function prevSig(m, i) {
    var j = i - 1;
    while (j >= 0 && /\s/.test(m.charAt(j))) j--;
    return j >= 0 ? m.charAt(j) : '';
  }

  /* ---------------- 2. 结构识别 ---------------- */
  var STR_ARRAY_SRC = '\\[\\s*(?:\'[^\']*\'|"[^"]*")\\s*(?:,\\s*(?:\'[^\']*\'|"[^"]*")\\s*)*,?\\s*\\]';

  function isSimpleWrapper(body) {
    if (body.length > 800) return false;
    if (body.indexOf('=>') >= 0) return false;
    if (/\b(?:if|for|while|switch|try|catch|throw|function|class|await|yield)\b/.test(body)) return false;
    if ((body.match(/return/g) || []).length !== 1) return false;
    return new RegExp('return\\s+' + IDENT + '\\s*\\(').test(body);
  }

  function collectAllFunctions(m) {
    var out = [];
    var re = /function\s+([A-Za-z_$][\w$]*)\s*\(/g, mt;
    while ((mt = re.exec(m)) !== null) {
      var start = mt.index;
      if (prevSig(m, start) === '.') continue;
      var p = mt.index + mt[0].length - 1;
      var cp = matchPair(m, p);
      if (cp < 0) continue;
      var b = skipWs(m, cp + 1);
      if (b >= m.length || m.charAt(b) !== '{') continue;
      var cb = matchPair(m, b);
      if (cb < 0) continue;
      out.push({ name: mt[1], start: start, end: cb + 1, bodyStart: b, bodyEnd: cb });
    }
    return out;
  }

  function findSeed(m, fns) {
    var re = new RegExp(STR_ARRAY_SRC, 'g'), mt;
    var best = null, bestCount = 0;
    while ((mt = re.exec(m)) !== null) {
      var count = mt[0].split(',').length;
      if (count < 3 || count <= bestCount) continue;
      var enc = null;
      for (var i = 0; i < fns.length; i++) {
        var f = fns[i];
        if (f.start <= mt.index && mt.index < f.end) {
          if (!enc || (f.end - f.start) < (enc.end - enc.start)) enc = f;
        }
      }
      if (!enc) continue;
      var body = m.slice(enc.bodyStart, enc.bodyEnd);
      var selfAssign = new RegExp('(?<![\\w$.])' + enc.name.replace(/\$/g, '\\$') + '\\s*=\\s*function');
      var selfReturn = new RegExp('return\\s+' + enc.name.replace(/\$/g, '\\$') + '\\s*\\(');
      if (selfAssign.test(body) || selfReturn.test(body)) {
        best = enc; bestCount = count;
      }
    }
    return best;
  }

  function collectVarDecls(m) {
    var decls = [];
    var re = /\b(?:var|let|const)\s+/g, mt, n = m.length;
    while ((mt = re.exec(m)) !== null) {
      var start = mt.index;
      var i = mt.index + mt[0].length;
      var declarators = [], ok = true;
      while (i < n) {
        var rest = m.slice(i, i + 200);
        var nm = /^([A-Za-z_$][\w$]*)/.exec(rest);
        if (!nm) { ok = false; break; }
        var name = nm[1];
        i = skipWs(m, i + nm[0].length);
        var rhsStart = null;
        if (i < n && m.charAt(i) === '=') {
          i = skipWs(m, i + 1);
          rhsStart = i;
          var depth = 0;
          while (i < n) {
            var ch = m.charAt(i);
            if (ch === '(' || ch === '[' || ch === '{') depth++;
            else if (ch === ')' || ch === ']' || ch === '}') {
              if (depth === 0) break;
              depth--;
            } else if (depth === 0 && (ch === ',' || ch === ';')) break;
            i++;
          }
        }
        declarators.push({ name: name, rhsStart: rhsStart, rhsEnd: i });
        i = skipWs(m, i);
        if (i < n && m.charAt(i) === ',') { i = skipWs(m, i + 1); continue; }
        if (i < n && m.charAt(i) === ';') i++;
        break;
      }
      if (ok && declarators.length) decls.push({ start: start, end: i, declarators: declarators });
      re.lastIndex = Math.max(re.lastIndex, i);
    }
    return decls;
  }

  function declText(m, decl, k) {
    var d = decl.declarators[k];
    if (d.rhsStart === null) return '';
    return m.slice(d.rhsStart, d.rhsEnd).trim();
  }

  function collectScaffold(src, m) {
    var fns = collectAllFunctions(m);
    var decls = collectVarDecls(m);
    var seed = findSeed(m, fns);

    var included = {}, names = {}, nameOrder = [];
    function addName(nm) { if (!names[nm]) { names[nm] = 1; nameOrder.push(nm); } }

    if (seed) {
      included[seed.name] = seed; addName(seed.name);

      var bodies = {}, callsOf = {};
      fns.forEach(function (f) {
        bodies[f.name] = m.slice(f.bodyStart, f.bodyEnd + 1);
        var re = new RegExp('(?<![\\w$.])(' + IDENT + ')\\s*\\(', 'g'), mm, set = {};
        while ((mm = re.exec(bodies[f.name])) !== null) set[mm[1]] = 1;
        callsOf[f.name] = set;
      });

      var rev = {};
      Object.keys(callsOf).forEach(function (nm) {
        Object.keys(callsOf[nm]).forEach(function (c) {
          if (!rev[c]) rev[c] = [];
          rev[c].push(nm);
        });
      });

      (rev[seed.name] || []).forEach(function (nm) {
        if (!included[nm]) {
          var f = null;
          for (var i = 0; i < fns.length; i++) if (fns[i].name === nm) { f = fns[i]; break; }
          if (f) { included[nm] = f; addName(nm); }
        }
      });

      var changed = true;
      while (changed) {
        changed = false;
        for (var i2 = 0; i2 < fns.length; i2++) {
          var f2 = fns[i2];
          if (names[f2.name]) continue;
          if (!isSimpleWrapper(bodies[f2.name])) continue;
          var called = callsOf[f2.name], hit = false;
          for (var nm2 in called) if (names[nm2]) { hit = true; break; }
          if (hit) { included[f2.name] = f2; addName(f2.name); changed = true; }
        }
      }
    }

    var chunks = [];
    Object.keys(included).forEach(function (nm) {
      var f = included[nm], ps = prevSig(m, f.start);
      chunks.push({ start: f.start, end: f.end, kind: 'decl', name: f.name,
        noRm: ['', ';', '}', '{'].indexOf(ps) < 0 });
    });

    /* 别名：const X = 已知标识符 */
    var changed2 = true;
    while (changed2) {
      changed2 = false;
      for (var d = 0; d < decls.length; d++) {
        for (var k = 0; k < decls[d].declarators.length; k++) {
          var nm3 = decls[d].declarators[k].name;
          if (names[nm3]) continue;
          var rhs = declText(m, decls[d], k);
          if (names[rhs]) {
            var ps2 = prevSig(m, decls[d].start);
            chunks.push({ start: decls[d].start, end: decls[d].end, kind: 'decl', name: nm3,
              noRm: ['', ';', '}', '{', ','].indexOf(ps2) < 0 });
            addName(nm3);
            changed2 = true;
          }
        }
      }
    }

    /* 轮转 / 自保护 IIFE：函数体里调用了已知函数 */
    var reIife = /\(\s*function\b/g, mt2;
    var nameList = nameOrder.slice();
    while ((mt2 = reIife.exec(m)) !== null) {
      var start = mt2.index;
      var ps3 = prevSig(m, start);
      if (['', ';', '}', ')', '=', ',', ':', '!', '&', '|', '?'].indexOf(ps3) < 0) continue;
      var fnKw = m.indexOf('function', start);
      var p = m.indexOf('(', fnKw);
      if (p < 0) continue;
      var cp = matchPair(m, p);
      if (cp < 0) continue;
      var b = skipWs(m, cp + 1);
      if (b >= m.length || m.charAt(b) !== '{') continue;
      var cb = matchPair(m, b);
      if (cb < 0) continue;
      var body = m.slice(b, cb);
      var callsDecoder = false;
      for (var z2 = 0; z2 < nameList.length; z2++) {
        if (new RegExp('(?<![\\w$.])' + nameList[z2].replace(/\$/g, '\\$') + '\\s*\\(').test(body)) {
          callsDecoder = true; break;
        }
      }
      if (!callsDecoder) continue;

      var end = cb + 1;
      for (var it = 0; it < 4; it++) {
        var k2 = skipWs(m, end);
        if (k2 < m.length && m.charAt(k2) === ')') { end = k2 + 1; continue; }
        if (k2 < m.length && m.charAt(k2) === '(') {
          var cc = matchPair(m, k2);
          if (cc < 0) break;
          end = cc + 1; continue;
        }
        break;
      }

      var seg = m.slice(start, end);
      var override = null, rmStart = null, rmEnd = null;
      var openCnt = (seg.match(/\(/g) || []).length;
      var closeCnt = (seg.match(/\)/g) || []).length;
      if (openCnt !== closeCnt) {
        override = '(' + src.slice(start + 1, end) + ');';
        var k3 = skipWs(m, end);
        if (k3 < m.length && m.charAt(k3) === ',') { rmStart = start + 1; rmEnd = k3 + 1; }
      }
      var ps4 = prevSig(m, start);
      chunks.push({ start: start, end: end, kind: 'iife', name: null,
        code: override, rmStart: rmStart, rmEnd: rmEnd,
        noRm: ['', ';', '}', '{'].indexOf(ps4) < 0 });
    }

    /* 去重 + 去掉被更大块包含的块 */
    chunks.sort(function (a, b) { return a.start - b.start || b.end - a.end; });
    var uniq = [];
    chunks.forEach(function (c) {
      for (var i = 0; i < uniq.length; i++) {
        var o = uniq[i];
        if (o.start <= c.start && c.end <= o.end) return;
      }
      uniq.push(c);
    });

    var nameSet = {};
    nameOrder.forEach(function (n2) { nameSet[n2] = 1; });
    return { chunks: uniq, names: nameSet, nameList: nameOrder };
  }

  /* ---------------- 3. 调用点 / 编辑 ---------------- */
  function isConstArgs(t) {
    var i = 0, n = t.length, depth = 0, seen = false;
    while (i < n) {
      var c = t.charAt(i);
      if (/\s/.test(c)) { i++; continue; }
      if (c === '(') { depth++; i++; continue; }
      if (c === ')') { depth--; if (depth < 0) return false; i++; continue; }
      if ('+-*/%,!~'.indexOf(c) >= 0) { i++; continue; }

      var rest = t.slice(i, i + 400), mt;
      if ((mt = /^(?:0[xX][0-9a-fA-F]+|(?:\d+\.\d*|\.\d+|\d+))/.exec(rest))) {
        seen = true; i += mt[0].length; continue;
      }
      if ((mt = /^(?:'[^']*'|"[^"]*")/.exec(rest))) {
        seen = true; i += mt[0].length; continue;
      }
      if ((mt = new RegExp('^(?:' + IDENT + '(?:\\s*\\.\\s*' + IDENT +
        '|\\s*\\[\\s*(?:0[xX][0-9a-fA-F]+|\\d+)\\s*\\])?)').exec(rest))) {
        seen = true; i += mt[0].length; continue;
      }
      return false;
    }
    return seen && depth === 0;
  }

  function mergeRanges(list) {
    var r = list.map(function (c) { return [c.start, c.end]; });
    r.sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; });
    var out = [];
    r.forEach(function (p) {
      if (out.length && p[0] <= out[out.length - 1][1]) {
        out[out.length - 1][1] = Math.max(out[out.length - 1][1], p[1]);
      } else out.push([p[0], p[1]]);
    });
    return out;
  }

  function chunkDefinedNames(m, chunk) {
    var text = m.slice(chunk.start, chunk.end), names = {}, order = [];
    var mt = /^\s*function\s+([A-Za-z_$][\w$]*)/.exec(text);
    if (mt) { names[mt[1]] = 1; return { set: names, list: [mt[1]] }; }
    var vt = /^\s*(?:var|let|const)\s+/.exec(text);
    if (!vt) return { set: names, list: order };
    var i = vt[0].length, n = text.length, depth = 0, expect = true;
    while (i < n) {
      var ch = text.charAt(i);
      if (ch === '(' || ch === '[' || ch === '{') depth++;
      else if (ch === ')' || ch === ']' || ch === '}') { depth--; if (depth < 0) break; }
      else if (depth === 0 && ch === ';') break;
      else if (depth === 0 && expect) {
        var mt2 = new RegExp('^(' + IDENT + ')').exec(text.slice(i, i + 200));
        if (mt2) {
          names[mt2[1]] = 1; order.push(mt2[1]);
          i += mt2[0].length; expect = false; continue;
        }
      } else if (depth === 0 && ch === ',') expect = true;
      i++;
    }
    return { set: names, list: order };
  }

  function findCalls(src, m, chunks, names) {
    var keys = Object.keys(names);
    if (!keys.length) return [];
    keys.sort(function (a, b) { return b.length - a.length; });
    var alt = keys.map(function (k) { return k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }).join('|');
    var re = new RegExp('(?<![\\w$.])(' + alt + ')\\s*\\(', 'g');

    var ranges = chunks.map(function (c) { return [c.start, c.end]; });
    function excluded(idx) {
      for (var i = 0; i < ranges.length; i++) if (ranges[i][0] <= idx && idx < ranges[i][1]) return true;
      return false;
    }

    var calls = [], mt;
    while ((mt = re.exec(m)) !== null) {
      var start = mt.index;
      if (excluded(start)) continue;
      var pre = m.slice(Math.max(0, start - 12), start);
      if (/function\s+$/.test(pre)) continue;
      var popen = mt.index + mt[0].length - 1;
      var close = matchPair(m, popen);
      if (close < 0) continue;
      var argsMask = m.slice(popen + 1, close);
      if (!argsMask.trim() || !isConstArgs(argsMask)) continue;
      calls.push({ name: mt[1], args: src.slice(popen + 1, close).trim(), start: start, end: close + 1 });
    }
    return calls;
  }

  function prepareChunkCode(src, c) {
    var code = c.code || src.slice(c.start, c.end);
    if (c.kind === 'decl') code = code.replace(/^(var|let|const)\b/, 'var');
    return code;
  }

  function applyEdits(text, edits) {
    edits = edits.slice().sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; });
    var out = text;
    for (var i = edits.length - 1; i >= 0; i--) {
      var e = edits[i];
      out = out.slice(0, e[0]) + e[2] + out.slice(e[1]);
    }
    return out;
  }

  /* ---------------- 4. 在隔离作用域执行解码器 ---------------- */
  /* 注意：解码必须运行在“非严格模式”的独立函数作用域里，直接 eval 才会把
     function/var 声明挂到同一个作用域，后续 eval 才能看到它们。
     因此这里用 new Function 生成 runner，而不是在严格模式模块内直接 eval。 */
  var RUNNER_SRC = [
    'var __dRes = [];',
    'var __dFn = [], __dOther = [], __dIife = [];',
    'for (var __dI = 0; __dI < chunks.length; __dI++) {',
    '  var __dC = chunks[__dI];',
    "  if (__dC.kind === 'iife') __dIife.push(__dC.code);",
    '  else if (/^\\s*function\\b/.test(__dC.code)) __dFn.push(__dC.code);',
    '  else __dOther.push(__dC.code);',
    '}',
    'for (var __dA = 0; __dA < __dFn.length; __dA++) { try { eval(__dFn[__dA]); } catch (e) {} }',
    'for (var __dB = 0; __dB < __dOther.length; __dB++) { try { eval(__dOther[__dB]); } catch (e) {} }',
    'for (var __dD = 0; __dD < __dOther.length; __dD++) { try { eval(__dOther[__dD]); } catch (e) {} }',
    'for (var __dF = 0; __dF < __dIife.length; __dF++) { try { eval(__dIife[__dF]); } catch (e) {} }',
    'for (var __dX = 0; __dX < calls.length; __dX++) {',
    '  try {',
    '    var __dCall = calls[__dX];',
    '    var __dFnRef = eval(__dCall.name);',
    '    if (typeof __dFnRef !== "function") { __dRes.push(null); continue; }',
    '    var __dV = eval("(" + __dCall.name + ")(" + __dCall.args + ")");',
    '    __dRes.push(typeof __dV === "string" ? __dV : null);',
    '  } catch (e) { __dRes.push(null); }',
    '}',
    'return __dRes;'
  ].join('\n');

  var runDecoder = new Function('chunks', 'calls', RUNNER_SRC);

  /* ---------------- 5. 主流程 ---------------- */
  function syntaxOk(code) {
    try { new Function(code); return true; } catch (e) { return false; }
  }

  function deobfuscateJS(src, clean) {
    if (clean === undefined) clean = true;

    var m = maskJs(src);
    var sc = collectScaffold(src, m);
    var chunks = sc.chunks;
    var names = sc.names;

    var preCalls = findCalls(src, m, chunks, names);

    /* 参数里引用的对象（如 _0x4506c1._0x11d03b）：把其声明也送进解码作用域 */
    var refNames = {};
    preCalls.forEach(function (c) {
      var re = new RegExp(IDENT, 'g'), mt;
      while ((mt = re.exec(c.args)) !== null) if (!names[mt[0]]) refNames[mt[0]] = 1;
    });
    if (Object.keys(refNames).length) {
      var decls = collectVarDecls(m);
      decls.forEach(function (d) {
        var hit = d.declarators.some(function (dd) { return refNames[dd.name]; });
        if (!hit) return;
        var overlap = chunks.some(function (c) { return !(d.end <= c.start || c.end <= d.start); });
        if (overlap) return;
        chunks.push({ start: d.start, end: d.end, kind: 'decl', name: null, noRm: false });
        d.declarators.forEach(function (dd) { if (refNames[dd.name]) names[dd.name] = 1; });
      });
      chunks.sort(function (a, b) { return a.start - b.start; });
    }

    var calls = findCalls(src, m, chunks, names);

    var keys = [], keyMap = {};
    calls.forEach(function (c) {
      var k = c.name + '\u0000' + c.args;
      if (!keyMap[k]) { keyMap[k] = 1; keys.push([c.name, c.args]); }
    });

    var payload = chunks.map(function (c) { return { kind: c.kind, code: prepareChunkCode(src, c) }; });
    var values = keys.length ? runDecoder(payload, keys.map(function (k) {
      return { name: k[0], args: k[1] };
    })) : [];

    var mapping = {};
    for (var v = 0; v < keys.length; v++) {
      if (typeof values[v] === 'string') mapping[keys[v][0] + '\u0000' + keys[v][1]] = values[v];
    }

    var replEdits = [], unresolved = [], seenUnresolved = {};
    var replaced = 0;
    calls.forEach(function (c) {
      var k = c.name + '\u0000' + c.args;
      var val = mapping[k];
      if (val === undefined) {
        if (!seenUnresolved[k]) { seenUnresolved[k] = 1; unresolved.push([c.name, c.args]); }
        return;
      }
      replEdits.push([c.start, c.end, JSON.stringify(val)]);
      replaced++;
    });

    /* 仍被正文引用的脚手架必须保留 */
    var kept = [];
    if (clean && chunks.length) {
      var blank = maskJs(src).split('');
      mergeRanges(chunks).forEach(function (r) {
        for (var i = r[0]; i < r[1]; i++) blank[i] = ' ';
      });
      var bodyView = applyEdits(blank.join(''), replEdits);

      var cnames = chunks.map(function (c) { return chunkDefinedNames(m, c); });
      var keptSet = {};
      chunks.forEach(function (c, idx) { if (c.noRm) keptSet[idx] = 1; });

      /* 反复传播：块被正文/其它保留块引用 => 保留；有保留块时轮转 IIFE 也要保留 */
      var guard = 0;
      while (guard++ < 6) {
        var before = Object.keys(keptSet).length;
        var changed = true;
        while (changed) {
          changed = false;
          for (var idx = 0; idx < chunks.length; idx++) {
            if (keptSet[idx]) continue;
            var ns = cnames[idx].list;
            if (!ns.length) continue;
            var ref = ns.some(function (nm) {
              return new RegExp('(?<![\\w$])' + nm.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(?![\\w$])').test(bodyView);
            });
            if (!ref) {
              for (var jdx in keptSet) {
                var cj = chunks[jdx];
                if (cj && ns.some(function (nm) {
                  return new RegExp('(?<![\\w$])' + nm.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(?![\\w$])')
                    .test(m.slice(cj.start, cj.end));
                })) { ref = true; break; }
              }
            }
            if (ref) { keptSet[idx] = 1; changed = true; }
          }
        }
        if (Object.keys(keptSet).length) {
          chunks.forEach(function (c, idx2) { if (c.kind === 'iife') keptSet[idx2] = 1; });
        }
        if (Object.keys(keptSet).length === before) break;
      }
      kept = Object.keys(keptSet).map(function (i) { return chunks[+i]; });
    }

    var removals = [];
    if (clean && chunks.length) {
      var removed = [];
      chunks.forEach(function (c) {
        if (kept.indexOf(c) >= 0) return;
        var a = c.rmStart, b = c.rmEnd;
        if (a === null || a === undefined) { a = c.start; b = c.end; }
        removed.push({ start: a, end: b });
      });
      mergeRanges(removed).forEach(function (r) {
        var hit = kept.some(function (o) { return !(r[1] <= o.start || o.end <= r[0]); });
        if (!hit) removals.push([r[0], r[1], '']);
      });
    }

    var edits = replEdits.concat(removals);
    edits.sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; });
    var safe = [], prevEnd = -1;
    edits.forEach(function (e) {
      if (e[0] < prevEnd) return;
      safe.push(e); prevEnd = e[1];
    });

    var out = applyEdits(src, safe);
    if (removals.length) {
      out = out.replace(/\n{3,}/g, '\n\n').replace(/;\s*;/g, ';');
    }

    var origOk = syntaxOk(src);
    var ok = origOk ? syntaxOk(out) : true;
    var note = '';
    if (!ok) {
      out = applyEdits(src, replEdits);
      ok = syntaxOk(out);
      note = '清理后语法校验失败，已自动退回“仅替换”模式';
      if (!ok) note += '；退回后仍无法通过校验，请改用 --no-clean / 手工处理';
    }

    var mapList = keys.map(function (k) {
      var key = k[0] + '\u0000' + k[1];
      return { call: k[0] + '(' + k[1] + ')', value: mapping[key] === undefined ? null : mapping[key] };
    });

    return {
      out: out,
      stats: {
        chunks: chunks.length,
        keptChunks: kept.length,
        calls: calls.length,
        uniqueCalls: keys.length,
        resolved: Object.keys(mapping).length,
        replaced: replaced,
        unresolved: unresolved.length,
        syntaxOk: ok,
        note: note,
        mapping: mapList,
        unresolvedList: unresolved.map(function (u) { return { call: u[0] + '(' + u[1] + ')' }; })
      }
    };
  }

  root.deobfuscateJS = deobfuscateJS;
  if (typeof module !== 'undefined' && module.exports) module.exports = { deobfuscateJS: deobfuscateJS };
})(typeof self !== 'undefined' ? self : this);
