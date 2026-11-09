# Agent Note: Material tokenizer runtime safety

Status: implemented

## Problem

[材料 Token 估算接口](../feature/2026-10-19-material-token-estimation-api.zh.md)
依赖 `tiktoken` 在运行期加载 `cl100k_base`。后端镜像只安装了 Python 包，没有包含
编码词表，因此离线或无法访问外网的部署会退化为 `len(text) // 4`。该比例对英文尚可，
但会把中文材料低估数倍。

另有两个问题让降级结果不可靠。文档正文中的 `<|endoftext|>` 等字面量会让
`Encoding.encode()` 拒绝整段文本，尽管它们只是普通材料内容。由此产生的字符降级
又因只跟踪初始化加载失败而被报告为 `fallback=false`。此外，进程级缓存会让一次瞬时
启动/加载失败留下的降级实例持续到后端重启。

## Decision

后端镜像现在会在构建期下载 `cl100k_base`，把词表复制到运行镜像，并设置
`TIKTOKEN_CACHE_DIR=/opt/tiktoken`。因此运行期 token 计数不再依赖外网访问。

文档正文使用 `disallowed_special=()` 编码，把看似特殊 token 的字符串当作普通内容。
`TokenEstimator` 会报告每段文本是否使用字符降级，材料接口再把逐文档状态汇总到响应的
`fallback` 字段。

材料路由改为每次请求创建轻量估算器，不再进程级缓存包装器。
`tiktoken.get_encoding()` 仍会缓存已加载的 BPE 对象，因此成功请求继续复用词表，
而瞬时加载失败可以在后续请求中恢复。

紧急降级改为 ASCII 约 4 字符/token、非 ASCII 约 1 字符/token。它仍是近似值，但不会
再把中文文本估成小四倍的结果。

## Alternatives considered

**继续在首次请求时下载词表。** 拒绝：离线和内网部署仍会返回同样错误的中文估算，
只是由 `fallback` 标记提示问题。

**把字面特殊 token 字符串当成 tiktoken 特殊 token。** 拒绝：文档正文属于用户材料，
不是协议控制数据。`cl100k_base` 只有一个 `<|endoftext|>` 特殊 token，也会比按可见
字符计数更少。

**继续进程级缓存估算器包装器。** 拒绝：首次网络失败会永久保留。按请求重试包装器可以
恢复，同时底层 tiktoken registry 继续缓存成功构造的编码器。

**所有降级继续使用 `len(text) // 4`。** 拒绝：接口用于中文知识库材料，该启发式会把
18 万字符的文档估成约 4.5 万 token，而实际通常至少约 10 万。

## Consequences

运行镜像会增大一个词表文件，未压缩约 1.6 MiB，压缩后小于 1 MiB。离线部署现在无需
先访问 `openaipublic.blob.core.windows.net` 就能执行确定性的 `cl100k_base` 计数。

响应的 `fallback` 字段现在描述请求实际执行的工作，包括意外编码错误后的字符降级。
接口仍估算 `cl100k_base`，而不是任意下游模型的 tokenizer；模型相关计数仍是当前契约
的明确限制。

## Testing

`backend/tests/test_token_estimator.py` 覆盖字面特殊 token 内容、CJK/ASCII 降级计数
和空字符串状态。`backend/tests/test_material_token_estimate_api.py` 在原有范围、排序、
父块、授权和不存在语义之外，新增逐文档降级的汇总报告测试。

后端镜像已在无网络运行环境中验证：能够加载随镜像复制的词表、返回
`fallback=false`、把 `<|endoftext|>` 计为 7 个 token，并把 320 字符中文样例计为
300 个 token。
