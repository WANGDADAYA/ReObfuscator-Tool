#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
obfuscator_deobfuscate.py
=========================
还原 javascript-obfuscator (obfuscator.io) 的字符串表（stringArray）。

原理
----
obfuscator.io 的字符串保护由若干“脚手架”组成：
  1. 字符串数组函数   function _0xAAA(){ const _0xBBB=[...]; ...; return _0xBBB; }
  2. 解码器           function _0xCCC(idx, key){ ... base64 / rc4 / rc4+base64 ... }
  3. 数组轮转 IIFE    (function(a, b){ while(!![]){ ... } })(_0xAAA, 0x12345);
  4. 包装器 wrappers  function _0xDDD(a, b){ return _0xCCC(a - 0x12, b); }
  5. 别名             var _0xEEE = _0xCCC;

本工具不去逆向每种编码算法：它把脚手架原样放进一个隔离的 Node vm 执行，
再用代码中真实的调用参数调用解码器，最后把调用点就地替换成字符串字面量。
所以 base64 / rc4 / rc4+base64 / 无编码 / 数组轮转 / 多重包装器 / 自保护 都能处理。

脚手架靠“结构”识别（字符串数组函数 -> 解码器 -> 单语句包装器 -> 别名），
因此不依赖 _0x 前缀，mangled 短名（a、b、c…）同样适用。

用法
----
  python3 obfuscator_deobfuscate.py input.js -o out.js
  python3 obfuscator_deobfuscate.py input.js -o out.js --map map.json --report
  python3 obfuscator_deobfuscate.py input.js -o out.js --no-clean    # 只替换，不删脚手架
  cat input.js | python3 obfuscator_deobfuscate.py - -o out.js

依赖：node（执行解码器）。其余全部由 Python 完成。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

KEYWORDS_BEFORE_REGEX = {
    "return", "typeof", "case", "in", "of", "new", "delete", "void",
    "instanceof", "do", "else", "yield", "await",
}
IDENT = r"[A-Za-z_$][\w$]*"


# ---------------------------------------------------------------------------
# 1. 词法遮罩：字符串/注释/正则内部替换为空格（保留引号），长度不变
# ---------------------------------------------------------------------------
def mask_js(src: str) -> str:
    n = len(src)
    mask = [" "] * n
    i = 0
    last_sig = ""
    last_word = ""

    while i < n:
        c = src[i]

        if c.isspace():
            i += 1
            continue

        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue

        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue

        if c in "\"'":
            q = c
            qpos = i
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == q:
                    i += 1
                    break
                i += 1
            mask[qpos] = q
            mask[min(i - 1, n - 1)] = q
            last_sig, last_word = "a", ""
            continue

        if c == "`":
            qpos = i
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == "`":
                    i += 1
                    break
                i += 1
            mask[qpos] = c
            mask[min(i - 1, n - 1)] = c
            last_sig, last_word = "a", ""
            continue

        if c == "/":
            is_regex = (
                last_sig in ("", "=", "(", "[", "{", ",", ";", ":", "!", "&", "|",
                             "?", "+", "-", "*", "%", "^", "~", "<", ">", "}")
                or last_word in KEYWORDS_BEFORE_REGEX
            )
            if is_regex:
                i += 1
                in_class = False
                while i < n:
                    ch = src[i]
                    if ch == "\\":
                        i += 2
                        continue
                    if ch == "[":
                        in_class = True
                    elif ch == "]":
                        in_class = False
                    elif ch == "/" and not in_class:
                        i += 1
                        break
                    elif ch == "\n":
                        break
                    i += 1
                while i < n and src[i].isalpha():
                    i += 1
                last_sig, last_word = "/", ""
                continue

        if c.isalpha() or c in "_$":
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            for k in range(i, j):
                mask[k] = src[k]
            last_word, last_sig = src[i:j], "a"
            i = j
            continue

        mask[i] = c
        last_sig, last_word = c, ""
        i += 1

    return "".join(mask)


def skip_ws(m: str, i: int) -> int:
    n = len(m)
    while i < n and m[i].isspace():
        i += 1
    return i


def match_pair(m: str, i: int) -> int:
    pairs = {"(": ")", "[": "]", "{": "}"}
    open_ch = m[i]
    close_ch = pairs[open_ch]
    depth = 0
    n = len(m)
    while i < n:
        if m[i] == open_ch:
            depth += 1
        elif m[i] == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def prev_sig(m: str, i: int) -> str:
    j = i - 1
    while j >= 0 and m[j].isspace():
        j -= 1
    return m[j] if j >= 0 else ""


# ---------------------------------------------------------------------------
# 2. 结构识别：字符串数组函数 -> 解码器 -> 包装器 -> 别名
# ---------------------------------------------------------------------------
STR_ARRAY_RE = re.compile(
    r"\[\s*(?:'[^']*'|\"[^\"]*\")\s*(?:,\s*(?:'[^']*'|\"[^\"]*\")\s*)*,?\s*\]")

WRAPPER_ILLEGAL_RE = re.compile(
    r"\b(?:if|for|while|switch|try|catch|throw|function|class|await|yield)\b")


def is_simple_wrapper(body: str) -> bool:
    """判断函数体是否为“只做一次转发调用”的包装器。

    允许的形式（含局部常量对象）：
      function W(a,b){ return DEC(a-0x1,b); }
      function W(a,b){ const o={x:3}; return DEC(a-o.x,b); }
    拒绝含分支/循环/嵌套函数的长函数体，避免把业务函数当成解码器。
    """
    if len(body) > 800:
        return False
    if "=>" in body or WRAPPER_ILLEGAL_RE.search(body):
        return False
    if body.count("return") != 1:
        return False
    return bool(re.search(r"return\s+" + IDENT + r"\s*\(", body))


def collect_all_functions(m: str):
    fns = []
    for mt in re.finditer(r"function\s+(" + IDENT + r")\s*\(", m):
        start = mt.start()
        if prev_sig(m, start) == ".":
            continue
        p = mt.end() - 1
        cp = match_pair(m, p)
        if cp < 0:
            continue
        b = skip_ws(m, cp + 1)
        if b >= len(m) or m[b] != "{":
            continue
        cb = match_pair(m, b)
        if cb < 0:
            continue
        fns.append({"name": mt.group(1), "start": start, "end": cb + 1,
                    "body_start": b, "body_end": cb})
    return fns


def find_seed(m: str, fns):
    """找字符串数组函数：函数体内有字符串数组字面量，并且自我替换或 return 自身。"""
    best, best_count = None, 0
    for mt in STR_ARRAY_RE.finditer(m):
        count = mt.group(0).count(",") + 1
        if count < 3 or count <= best_count:
            continue
        enc = [f for f in fns if f["start"] <= mt.start() < f["end"]]
        if not enc:
            continue
        fn = min(enc, key=lambda x: x["end"] - x["start"])
        body = m[fn["body_start"]:fn["body_end"]]
        if (re.search(r"(?<![\w$.])%s\s*=\s*function" % re.escape(fn["name"]), body)
                or re.search(r"return\s+%s\s*\(" % re.escape(fn["name"]), body)):
            best, best_count = fn, count
    return best


def collect_var_decls(m: str):
    """收集 var/let/const 声明及其声明符（名、右值起点/种类）。"""
    decls = []
    n = len(m)
    for mt in re.finditer(r"\b(?:var|let|const)\s+", m):
        start = mt.start()
        i = mt.end()
        declarators = []
        ok = True
        while i < n:
            nm = re.match(IDENT, m[i:])
            if not nm:
                ok = False
                break
            name = nm.group(0)
            i = skip_ws(m, i + nm.end())
            rhs_start = None
            if i < n and m[i] == "=":
                i = skip_ws(m, i + 1)
                rhs_start = i
                depth = 0
                while i < n:
                    ch = m[i]
                    if ch in "([{":
                        depth += 1
                    elif ch in ")]}":
                        if depth == 0:
                            break
                        depth -= 1
                    elif depth == 0 and ch in ",;":
                        break
                    i += 1
            declarators.append({"name": name, "rhs_start": rhs_start, "rhs_end": i})
            i = skip_ws(m, i)
            if i < n and m[i] == ",":
                i = skip_ws(m, i + 1)
                continue
            if i < n and m[i] == ";":
                i += 1
            break
        if ok and declarators:
            decls.append({"start": start, "end": i, "declarators": declarators})
    return decls


def text_of_decl(m: str, decl, k) -> str:
    d = decl["declarators"][k]
    if d["rhs_start"] is None:
        return ""
    return m[d["rhs_start"]:d["rhs_end"]].strip()


def collect_scaffold(src: str, m: str):
    """返回 (chunks, callable_names)。chunk = {start,end,kind,name}。"""
    fns = collect_all_functions(m)
    decls = collect_var_decls(m)
    seed = find_seed(m, fns)

    included = {}          # name -> fn
    names = set()
    if seed:
        included[seed["name"]] = seed
        names.add(seed["name"])

        bodies = {f["name"]: m[f["body_start"]:f["body_end"] + 1] for f in fns}
        calls_of = {f["name"]: set(re.findall(
            r"(?<![\w$.])(" + IDENT + r")\s*\(", bodies[f["name"]])) for f in fns}
        rev = {}
        for nm, cs in calls_of.items():
            for c in cs:
                rev.setdefault(c, []).append(nm)

        # 第一跳：调用数组函数的函数（解码器）
        for nm in rev.get(seed["name"], []):
            if nm not in included:
                included[nm] = next(f for f in fns if f["name"] == nm)
                names.add(nm)

        # 后续跳：只接受“单条 return 调用”的包装器
        changed = True
        while changed:
            changed = False
            for f in fns:
                if f["name"] in names:
                    continue
                if not is_simple_wrapper(bodies[f["name"]]):
                    continue
                if calls_of[f["name"]] & names:
                    included[f["name"]] = f
                    names.add(f["name"])
                    changed = True

    chunks = []
    for f in included.values():
        ps = prev_sig(m, f["start"])
        chunks.append({"start": f["start"], "end": f["end"], "kind": "decl",
                       "name": f["name"], "no_rm": ps not in ("", ";", "}", "{")})

    # 别名：const NAME = 已知标识符
    changed = True
    while changed:
        changed = False
        for d in decls:
            for k, dd in enumerate(d["declarators"]):
                nm = dd["name"]
                if nm in names:
                    continue
                rhs = text_of_decl(m, d, k)
                if rhs in names:
                    ps = prev_sig(m, d["start"])
                    chunks.append({"start": d["start"], "end": d["end"], "kind": "decl",
                                   "name": nm, "no_rm": ps not in ("", ";", "}", "{", ",")})
                    names.add(nm)
                    changed = True

    # 轮转 IIFE：函数体里调用了已知解码器/数组函数
    for mt in re.finditer(r"\(\s*function\b", m):
        start = mt.start()
        if prev_sig(m, start) not in ("", ";", "}", ")", "=", ",", ":", "!", "&", "|", "?"):
            continue
        fn_kw = m.index("function", start)
        p = m.find("(", fn_kw)
        if p < 0:
            continue
        cp = match_pair(m, p)
        if cp < 0:
            continue
        b = skip_ws(m, cp + 1)
        if b >= len(m) or m[b] != "{":
            continue
        cb = match_pair(m, b)
        if cb < 0:
            continue
        body = m[b:cb]
        if not any(re.search(r"(?<![\w$.])%s\s*\(" % re.escape(nm), body) for nm in names):
            continue
        # 结束位置：函数体后可能紧跟 () 调用、外层 ) 包裹，依次吞掉
        end = cb + 1
        for _ in range(4):
            k = skip_ws(m, end)
            if k < len(m) and m[k] == ")" or (k < len(m) and m[k] == "("):
                if m[k] == ")":
                    end = k + 1
                else:
                    cc = match_pair(m, k)
                    if cc < 0:
                        break
                    end = cc + 1
                continue
            break

        # 括号是否自平衡；(A, B) 逗号链里截出来的 (A 不自平衡
        segm = m[start:end]
        override = None
        rm_start = rm_end = None
        if segm.count("(") != segm.count(")"):
            override = "(" + src[start + 1:end] + ");"
            k2 = skip_ws(m, end)
            if k2 < len(m) and m[k2] == ",":
                rm_start, rm_end = start + 1, k2 + 1
        e2 = skip_ws(m, end)
        if e2 < len(m) and m[e2] == ";":
            end = e2 + 1
        ps = prev_sig(m, start)
        chunks.append({"start": start, "end": end, "kind": "iife", "name": None,
                       "code": override,
                       "rm_start": rm_start, "rm_end": rm_end,
                       "no_rm": ps not in ("", ";", "}", "{")})

    # 去重 + 去掉被更大块包含的块
    chunks.sort(key=lambda c: (c["start"], -c["end"]))
    uniq = []
    for c in chunks:
        if any(o["start"] <= c["start"] and c["end"] <= o["end"]
               and (o["start"], o["end"]) != (c["start"], c["end"]) for o in uniq):
            continue
        if any((o["start"], o["end"]) == (c["start"], c["end"]) for o in uniq):
            continue
        uniq.append(c)
    return uniq, names


# ---------------------------------------------------------------------------
# 3. 调用点识别：NAME(常量参数)
# ---------------------------------------------------------------------------
def is_const_args(t: str) -> bool:
    """参数是否全为常量（数字 / 字符串 / 运算符 / 已知引用），避免误执行 业务函数。"""
    i, n, depth, seen = 0, len(t), 0, False
    while i < n:
        c = t[i]
        if c.isspace():
            i += 1
            continue
        if c == "(":
            depth += 1
            i += 1
            continue
        if c == ")":
            depth -= 1
            if depth < 0:
                return False
            i += 1
            continue
        if c in "+-*/%,!~":
            i += 1
            continue
        mt = re.match(r"0[xX][0-9a-fA-F]+|(?:\d+\.\d*|\.\d+|\d+)", t[i:])
        if mt:
            seen = True
            i += mt.end()
            continue
        mt = re.match(r"'[^']*'|\"[^\"]*\"", t[i:])
        if mt:
            seen = True
            i += mt.end()
            continue
        mt = re.match(IDENT + r"(?:\s*\.\s*" + IDENT + r"|\s*\[\s*(?:0[xX][0-9a-fA-F]+|\d+)\s*\])?", t[i:])
        if mt:
            seen = True
            i += mt.end()
            continue
        return False
    return seen and depth == 0


def merge_ranges(chunks):
    ranges = sorted((c["start"], c["end"]) for c in chunks)
    merged = []
    for a, b in ranges:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def chunk_defined_names(m: str, chunk) -> set:
    text = m[chunk["start"]:chunk["end"]]
    names = set()
    mt = re.match(r"\s*function\s+(" + IDENT + r")", text)
    if mt:
        return {mt.group(1)}
    vt = re.match(r"\s*(?:var|let|const)\s+", text)
    if not vt:
        return names
    i, n, depth, expect_name = vt.end(), len(text), 0, True
    while i < n:
        ch = text[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                break
        elif depth == 0 and ch == ";":
            break
        elif depth == 0 and expect_name:
            mt2 = re.match(IDENT, text[i:])
            if mt2:
                names.add(mt2.group(0))
                i += mt2.end()
                expect_name = False
                continue
        elif depth == 0 and ch == ",":
            expect_name = True
        i += 1
    return names


def find_calls(src: str, m: str, chunks, callable_names):
    ranges = [(c["start"], c["end"]) for c in chunks]

    def excluded(idx):
        for a, b in ranges:
            if a <= idx < b:
                return True
        return False

    if not callable_names:
        return []
    alt = "|".join(re.escape(n) for n in sorted(callable_names, key=len, reverse=True))
    call_re = re.compile(r"(?<![\w$.])(" + alt + r")\s*\(")

    calls = []
    for mt in call_re.finditer(m):
        start = mt.start()
        if excluded(start):
            continue
        pre = m[max(0, start - 12):start]
        if re.search(r"function\s+$", pre):
            continue
        popen = mt.end() - 1
        close = match_pair(m, popen)
        if close < 0:
            continue
        args_mask = m[popen + 1:close]
        if not args_mask.strip() or not is_const_args(args_mask):
            continue
        calls.append({"name": mt.group(1), "args": src[popen + 1:close].strip(),
                      "start": start, "end": close + 1})
    return calls


# ---------------------------------------------------------------------------
# 4. Node 侧执行
# ---------------------------------------------------------------------------
NODE_HARNESS = r"""
'use strict';
const vm = require('vm');
const fs = require('fs');
const payload = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

const ctx = vm.createContext({ console: { log(){}, warn(){}, error(){}, info(){}, debug(){}, trace(){} } });
const run = (code) => { try { vm.runInContext(code, ctx, { timeout: 4000 }); } catch (e) { } };

const all = payload.chunks.filter(c => c.kind === 'decl').map(c => c.code);
const iifes = payload.chunks.filter(c => c.kind === 'iife').map(c => c.code);

// 模拟函数声明提升：先 function 声明，再 var/别名（跑两轮，兼容解码器后置）
const fnDecls = all.filter(c => /^\s*function\b/.test(c));
const otherDecls = all.filter(c => !/^\s*function\b/.test(c));
for (const code of fnDecls) run(code);
for (const code of otherDecls) run(code);
for (const code of otherDecls) run(code);
for (const code of iifes) run(code);

const out = [];
for (const call of payload.calls) {
  try {
    const fn = vm.runInContext(call.name, ctx, { timeout: 1000 });
    if (typeof fn !== 'function') { out.push(null); continue; }
    const v = vm.runInContext('(' + call.name + ')(' + call.args + ')', ctx, { timeout: 1500 });
    out.push(typeof v === 'string' ? v : null);
  } catch (e) {
    out.push(null);
  }
}
process.stdout.write(JSON.stringify(out));
"""


def node_decode(chunk_code, calls):
    node = shutil.which("node")
    if not node:
        sys.exit("未找到 node：执行解码器需要 Node.js 环境。")
    payload = {"chunks": [{"kind": k, "code": c} for k, c in chunk_code],
               "calls": [{"name": n, "args": a} for n, a in calls]}
    tmpdir = tempfile.mkdtemp(prefix="deobf_")
    try:
        p_payload = os.path.join(tmpdir, "payload.json")
        p_harness = os.path.join(tmpdir, "harness.js")
        with open(p_payload, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        with open(p_harness, "w", encoding="utf-8") as f:
            f.write(NODE_HARNESS)
        proc = subprocess.run([node, p_harness, p_payload], capture_output=True,
                              text=True, timeout=300)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr or "")
            sys.exit("node 执行失败。")
        return json.loads(proc.stdout or "[]")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def node_check(code: str):
    node = shutil.which("node")
    if not node:
        return True, ""
    tmpdir = tempfile.mkdtemp(prefix="deobf_chk_")
    try:
        p = os.path.join(tmpdir, "check.js")
        with open(p, "w", encoding="utf-8") as f:
            f.write(code)
        proc = subprocess.run([node, "--check", p], capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            return True, ""
        err = (proc.stderr or "").strip().splitlines()
        return False, (err[4] if len(err) > 4 else (err[0] if err else "syntax error"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def js_literal(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def prepare_chunk_code(src, c):
    code = c.get("code") or src[c["start"]:c["end"]]
    if c["kind"] == "decl":
        code = re.sub(r"^(var|let|const)\b", "var", code, count=1)
    return code


def apply_edits(text: str, edits) -> str:
    out = text
    for a, b, t in reversed(sorted(edits, key=lambda e: (e[0], e[1]))):
        out = out[:a] + t + out[b:]
    return out


# ---------------------------------------------------------------------------
# 5. 主流程
# ---------------------------------------------------------------------------
def deobfuscate(src: str, clean: bool = True):
    m = mask_js(src)
    chunks, names = collect_scaffold(src, m)

    # 参数里引用了某个对象（如 _0x4506c1._0x11d03b）时，把它的声明也送进沙箱
    pre_calls = find_calls(src, m, chunks, names)
    extra_decls = []
    ref_names = set()
    for c in pre_calls:
        for idm in re.finditer(IDENT, c["args"]):
            ident = idm.group(0)
            if ident in names:
                continue
            ref_names.add(ident)
    if ref_names:
        for d in collect_var_decls(m):
            if any(dd["name"] in ref_names for dd in d["declarators"]):
                extra_decls.append({"start": d["start"], "end": d["end"],
                                    "kind": "decl", "name": None})
        for c in extra_decls:
            if not any(not (c["end"] <= o["start"] or o["end"] <= c["start"]) for o in chunks):
                chunks.append(c)
        chunks.sort(key=lambda c: c["start"])
        names |= {dd["name"] for d in collect_var_decls(m) for dd in d["declarators"]
                  if dd["name"] in ref_names}

    calls = find_calls(src, m, chunks, names)

    keys = []
    for c in calls:
        k = (c["name"], c["args"])
        if k not in keys:
            keys.append(k)

    chunk_code = [(c["kind"], prepare_chunk_code(src, c)) for c in chunks]
    values = node_decode(chunk_code, keys) if keys else []

    mapping = {}
    for k, v in zip(keys, values):
        if isinstance(v, str):
            mapping[k] = v

    repl_edits = []
    unresolved = []
    replaced = 0
    for c in calls:
        key = (c["name"], c["args"])
        v = mapping.get(key)
        if v is None:
            if key not in unresolved:
                unresolved.append(key)
            continue
        repl_edits.append((c["start"], c["end"], js_literal(v)))
        replaced += 1

    # 计算仍被正文引用的脚手架块（删掉会报错，必须保留）
    kept = []
    if clean and chunks:
        blank = list(m)
        for a, b in merge_ranges(chunks):
            for k in range(a, b):
                blank[k] = " "
        body_view = apply_edits("".join(blank), repl_edits)

        cnames = {}
        for idx, c in enumerate(chunks):
            ns = chunk_defined_names(m, c)
            if c["name"]:
                ns.add(c["name"])
            cnames[idx] = ns

        kept_idx = {i for i, c in enumerate(chunks) if c.get("no_rm")}
        changed = True
        while changed:
            changed = False
            for idx, c in enumerate(chunks):
                if idx in kept_idx or not cnames[idx]:
                    continue
                ref = any(re.search(r"(?<![\w$])%s(?![\w$])" % re.escape(nm), body_view)
                          for nm in cnames[idx])
                if not ref:
                    for jdx in kept_idx:
                        if any(re.search(r"(?<![\w$])%s(?![\w$])" % re.escape(nm),
                                         m[chunks[jdx]["start"]:chunks[jdx]["end"]])
                               for nm in cnames[idx]):
                            ref = True
                            break
                if ref:
                    kept_idx.add(idx)
                    changed = True
        # 只要还有块保留，轮转 IIFE 必须保留（否则数组索引全错位）
        if kept_idx:
            for idx, c in enumerate(chunks):
                if c["kind"] == "iife":
                    kept_idx.add(idx)
        kept = [chunks[i] for i in sorted(kept_idx)]

    removals = []
    if clean and chunks:
        removed = []
        for c in chunks:
            if c in kept:
                continue
            a, b = c.get("rm_start"), c.get("rm_end")
            if a is None:
                a, b = c["start"], c["end"]
            removed.append({"start": a, "end": b})
        for a, b in merge_ranges(removed):
            if any(not (b <= o["start"] or o["end"] <= a) for o in kept):
                continue
            removals.append((a, b, ""))

    edits = repl_edits + removals
    edits.sort(key=lambda e: (e[0], e[1]))
    safe, prev_end = [], -1
    for a, b, t in edits:
        if a < prev_end:
            continue
        safe.append((a, b, t))
        prev_end = b

    out = apply_edits(src, safe)
    if removals:
        out = re.sub(r"\n{3,}", "\n\n", out)
        out = re.sub(r";\s*;", ";", out)

    ok, err = node_check(out)
    note = ""
    if not ok:
        out = apply_edits(src, repl_edits)
        ok2, err2 = node_check(out)
        note = "清理后语法校验失败(%s)，已退回“仅替换”模式" % err
        if not ok2:
            note += "；退回后仍不通过：%s" % err2

    stats = {
        "chunks": len(chunks),
        "kept_chunks": len(kept),
        "calls": len(calls),
        "unique_calls": len(keys),
        "resolved": len(mapping),
        "replaced": replaced,
        "unresolved": len(unresolved),
        "syntax_ok": ok,
        "note": note,
        "mapping": {f"{k[0]}({k[1]})": v for k, v in mapping.items()},
        "unresolved_list": [{"name": n, "args": a} for n, a in unresolved],
    }
    return out, stats


def main():
    ap = argparse.ArgumentParser(description="还原 javascript-obfuscator (obfuscator.io) 字符串表")
    ap.add_argument("input", help="混淆后的 js 文件；用 - 表示 stdin")
    ap.add_argument("-o", "--output", help="输出文件，默认 stdout")
    ap.add_argument("--map", metavar="FILE", help="把 调用→字符串 映射输出为 JSON")
    ap.add_argument("--report", action="store_true", help="打印统计信息到 stderr")
    ap.add_argument("--no-clean", action="store_true", help="保留脚手架，只做字符串替换")
    args = ap.parse_args()

    if args.input == "-":
        src = sys.stdin.read()
    else:
        with open(args.input, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()

    out, stats = deobfuscate(src, clean=not args.no_clean)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out)
        print("已写入 %s" % args.output, file=sys.stderr)
    else:
        sys.stdout.write(out)

    if args.map:
        with open(args.map, "w", encoding="utf-8") as f:
            json.dump(stats["mapping"], f, ensure_ascii=False, indent=2)
        print("映射已写入 %s" % args.map, file=sys.stderr)

    if args.report:
        print("\n[统计] 脚手架 %d 段（保留 %d）| 调用点 %d（去重 %d）| 还原 %d 条 | 替换 %d 处 | 未解析 %d 条 | 语法 %s"
              % (stats["chunks"], stats["kept_chunks"], stats["calls"], stats["unique_calls"],
                 stats["resolved"], stats["replaced"], stats["unresolved"],
                 "OK" if stats["syntax_ok"] else "FAIL"), file=sys.stderr)
        if stats["note"]:
            print("  提示: " + stats["note"], file=sys.stderr)
        for u in stats["unresolved_list"][:15]:
            print("  未解析: %s(%s)" % (u["name"], u["args"]), file=sys.stderr)


if __name__ == "__main__":
    main()
