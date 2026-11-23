# obfuscator.io 字符串表还原工具

针对 [javascript-obfuscator / obfuscator.io](https://obfuscator.io) `stringArray` 保护的字符串还原工具，提供 **HTML 单文件版**（浏览器直接用）与 **Python 命令行版**（批量处理）。

输入混淆代码，输出把 `_0x1a2b(0x2f)`、`_0x4dfe(0x1a8, 'k9')` 这类调用替换成原始字符串字面量的可读代码。

---

## 一、原理

obfuscator.io 的字符串保护由几段"脚手架"组成，通常位于文件前部/每个函数内部：

| 组成 | 形态 |
| --- | --- |
| 字符串数组函数 | `function _0xAAA(){ const _0xBBB=['…']; _0xAAA=function(){return _0xBBB}; return _0xAAA(); }` |
| 解码器 | `function _0xCCC(idx, key){ … base64 / rc4 / rc4+base64 … }` |
| 数组轮转 IIFE | `(function(a,b){ while(!![]){ … } })(_0xAAA, 0x12345)` |
| 包装器 wrappers | `function _0xDDD(a,b){ return _0xCCC(a-0x12,b); }` |
| 别名 | `var _0xEEE = _0xCCC;` |

工具**不逆向每种编码算法**，而是：

1. 用词法遮罩（字符串/注释/正则全部置空）扫描结构；
2. 结构化识别脚手架链：**字符串数组函数 → 解码器 → 单语句包装器 → 别名 → 轮转 IIFE**；
3. 把脚手架原样放进隔离作用域执行；
4. 用代码里**真实的调用参数**调用解码器，得到字符串；
5. 在正文里就地替换成 `JSON.stringify` 形式的字面量；
6. 判断哪些脚手架仍被正文引用（删掉会报错），其余删除；
7. 用 JS 引擎做语法校验，校验不通过自动退回"仅替换"模式。

因此 base64 / rc4 / rc4+base64 / 无编码 / 数组轮转 / 多重包装器 / selfDefending / controlFlowFlattening 都能处理，且**不依赖 `_0x` 前缀**——`mangled` 短名（`a`、`b`、`c`…）同样适用。

识别是结构驱动的：先找"函数体内有字符串数组字面量、且自我替换 `NAME=function(){return ARR}`"的函数作为种子，再顺着调用关系向外扩展。单语句转发（`return DEC(a-1,b)`）才被视为包装器，避免把业务函数误判成解码器。

---

## 二、HTML 版（推荐日常使用）

`obfuscator_deobfuscate.html` —— 纯前端单文件，双击即可打开，无需安装、无需联网。

1. 粘贴混淆代码，或把 `.js` 文件拖进左侧编辑框；
2. 点击 **还原字符串**（或 `Ctrl/⌘ + Enter`）；
3. 右侧得到还原结果，下方列出 **调用 → 字符串** 映射表（可过滤、点击行复制单条）；
4. 可 **复制结果** / **下载 .js** / **下载映射 JSON**。

选项 **删除脚手架**（默认开）：开关删除数组函数/解码器/轮转那段代码。关掉就是"只替换字符串，保留全部原结构"，适合脚手架删不干净时使用。

安全设计：解码器在**独立的 Web Worker** 中执行，先禁用 `fetch` / `XMLHttpRequest` / `WebSocket` / `importScripts`，30 秒超时自动终止。代码不会离开浏览器。

> 仍建议只处理你被授权分析的样本；混淆代码本质上是可执行代码，只能在隔离环境里跑。

---

## 三、Python 版（批量 / 自动化）

```bash
python3 obfuscator_deobfuscate.py input.js -o out.js
python3 obfuscator_deobfuscate.py input.js -o out.js --map map.json --report
python3 obfuscator_deobfuscate.py input.js -o out.js --no-clean   # 只替换，不删脚手架
cat input.js | python3 obfuscator_deobfuscate.py - -o out.js
```

| 参数 | 说明 |
| --- | --- |
| `input` / `-` | 混淆文件，`-` 表示 stdin |
| `-o, --output` | 输出文件（缺省打印到 stdout） |
| `--map FILE` | 导出 `调用 → 字符串` 的 JSON 映射 |
| `--report` | 打印统计与未解析项 |
| `--no-clean` | 保留脚手架，仅做字符串替换 |

依赖：只需 `node`（执行解码器）；扫描、替换、清理全部由 Python 完成。

扫描与替换是纯 Python 实现，解码执行在 Node `vm` 中完成（每个 chunk 4 秒、每次调用 1.5 秒超时），不会跑挂主进程。

---

## 四、实测结果

用 `javascript-obfuscator@4.2.2` 生成 12 种配置的样本（源码含中文、URL、签名拼接），外加一个 202 KB 的真实库（`validator.js` → 混淆后 252 KB），逐项对比**还原前后运行输出**：

| 样本 | 配置要点 | 还原 | 语法 | 运行结果 |
| --- | --- | --- | --- | --- |
| sample_base64 | base64 + 函数包装器链 | 13/13 | OK | 一致 |
| sample_rc4 | rc4 + 变量包装器 | 13/13 | OK | 一致 |
| sample_none | 无编码 + 阈值 0.75 + CFF + selfDefending + splitStrings | 25/25 | OK | 一致 |
| sample_b64_stringCalls | base64 + `stringArrayCallsTransform` | 13/13 | OK | 一致 |
| v1 | base64 + 3 层包装器 + chainedCalls | 13/13 | OK | 一致 |
| v2 | rc4 + variable 包装器 | 13/13 | OK | 一致 |
| v3 | 无编码 + 不轮转 | 13/13 | OK | 一致 |
| v4 | base64 + 阈值 0.5 | 6/6 | OK | 一致 |
| v5 | `renameGlobals` + mangled 短名 | 13/13 | OK | 一致 |
| v6 | rc4 + selfDefending + CFF + deadCode + splitStrings | 42/42 | OK | 一致 |
| v7 | base64 + numbersToExpressions | 13/13 | OK | 一致 |
| v8 | base64 + 非压缩输出 | 13/13 | OK | 一致 |
| big_b64 | 真实库 252 KB，base64 + 包装器 | 2850/2850 | OK | 一致（`isEmail`/`isURL` 正常） |

合计：**13/13 样本还原后运行输出与原程序完全一致，未解析 0 条。**

---

## 五、已知限制

1. **未解析的调用会原样保留**。常见原因：参数不是常量（引用了运行时变量）、动态拼接、或解码依赖文件外的全局变量。统计里会给出数量与前若干条明细。
2. **selfDefending / 自保护代码会被保留**。这类代码校验自身源码，强行删除会导致运行报错；工具选择保留并让字符串替换照常生效。
3. **"删不干净"是安全选择**。若某段脚手架仍被正文引用（例如 `(function(){…}())` 的返回值被赋给变量后继续调用），工具会保留它而不是冒险删除；此时会显示"保留 N 段"。
4. **死循环保护**：数组轮转若因参数异常不收敛，HTML 版 30 秒后终止 Worker，Python 版每个 chunk 4 秒超时。
5. 只处理字符串表。标识符还原、控制流平坦化还原、死代码消除不在范围内（可继续用 deobfuscator.io 等工具）。

---

## 六、文件清单

| 文件 | 说明 |
| --- | --- |
| `obfuscator_deobfuscate.html` | 单文件网页工具（内嵌引擎与示例） |
| `obfuscator_deobfuscate.py` | Python 命令行工具 |
| `engine.js` | 引擎源码（可单独引入/二次开发） |
| `sample_base64.js` | 测试用混淆样本 |
