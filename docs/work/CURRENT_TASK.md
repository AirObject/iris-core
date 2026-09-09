# 当前任务

任务：**日志事件安全处理、JSONL编码与等级路由：用户已验收；独立本地提交归档**。

## 基线与授权

2026-09-10，目录及Git根均为`/Users/cassia/Local/Code/iris_memory_core`，分支`main`，HEAD为`b94cd5070b9da8c2683e0f7632c248175095acf2`；实现起点工作区、暂存区及未跟踪清单均为空。用户确认该提交的配置校验切片验收结束，并授权本切片。已读取AGENTS、INDEX、STATUS、完整[代码规范](../CODING_STANDARDS.md)、[日志事件／编码／路由／错误顺序及验收例子](../architecture/logging.md#runtime-diagnostics-events)，以及配置接入所需契约与公开接口；reference不作为现行约束。

用户现已确认本日志切片验收通过，授权更新本文件和STATUS并创建独立本地提交。验收收尾起点仅有日志目录7份、测试目录6份新增Python及两份工作记录，共15份文件，暂存区为空，无额外改动；本次不修改源码／测试。提交仅包含这15份文件，标题为`feat(logging): 增加安全事件编码与等级路由`；既有配置源码、139项测试、工程配置、锁文件与已批准契约保持不变。无须改变配置公开行为；G2仍待定，不阻塞本轮纯内存组件，是生产装配前置。

## 实现与边界

- 内部步骤为顶层载体／全部键→level／event_code→规范化槽位接入边界→context／attributes两层形状与键→白名单值→ID／UTC时间→编码／大小→等级路由。只保留已核验原生值，未知对象不读取、遍历、转换或保留；固定模板、安全首错、嵌套不可变所有权及输入隔离已实现。
- ID与时钟由可信内部调用方注入，分别使用规范小写UUID文本与精确UTC datetime，失败不重试。JSONL按UTF-8字节计入尾部LF；主记录超限拒绝，格式化故障仅尝试一次同ID／时间的安全备用，备用失败返回固定错误；不执行应急输出。
- 配置适配器仅接收`resolve_configuration_with_logging_validation`成功结果，通过原生快照公开`get_entry`读取8项所需值，无私有配置调用、默认副本、路径保留或重新注册。该内部前置条件不替代完整initialize校验。模块精确匹配；解析实例／模块／端的NOTSET与禁用规则，按console、file顺序返回阈值判定及纯流选择；ELIGIBLE仅表示等级允许，不代表入队、写出或持久化。

## 当前版本的实际验证

对象为上述HEAD加本记录所属提交的实现／测试。上一轮未保存逐文件指纹，故本次验收收尾重新执行下列检查，不直接复用历史结果；Python 3.12.13、uv 0.12.9、项目锁定Pyright 1.1.411，无依赖安装。以下检查实际执行，退出码均为0：

```text
uv --cache-dir /tmp/iris-memory-core-uv-cache run --offline --no-sync pyright --version
uv --cache-dir /tmp/iris-memory-core-uv-cache run --offline --no-sync pyright --project pyproject.toml --pythonpath .venv/bin/python
uv --cache-dir /tmp/iris-memory-core-uv-cache run --offline --no-sync python -m unittest discover -s tests -t . -v
uv --cache-dir /tmp/iris-memory-core-uv-cache run --offline --no-sync python -m compileall -q companion_memory tests
uv --cache-dir /tmp/iris-memory-core-uv-cache lock --check --offline
git diff --check
git diff --cached --check
```

全量Pyright覆盖源码及测试40份Python文件（含13份新增未跟踪文件）：**0 errors／0 warnings／0 informations**；unittest **205项通过＝原139项＋新增66项**。路由矩阵覆盖5,040组已校验合成配置、25,200条事件的双端判定；其余覆盖类型／哈希／转换钩子、敏感字段移除与引用释放、错误顺序、输入隔离与深不可变、JSON转义／UTF-8及LF边界、备用失败、ID／时钟次数、无外部副作用。编译、离线锁文件检查通过；未查看编辑器Pylance诊断。

实现轮已对40份Python完成AST／模块说明检查；13份新增文件逐一完成`git diff --no-index --check /dev/null <文件>`、未用导入及跨模块公开接口扫描，新增差异的预期退出码1不作检查失败，均无空白诊断。实现轮人工核对处理顺序、所有权、语义命名及能力边界。本次检查前后及最终暂存逐一核对40份Python与3份共享配置／锁文件的SHA-256；已测内容一致，最终暂存清单仅含上述15份文件且差异检查通过。原配置目录与测试保持不变；提交版本由本记录所属提交定位。

## 限制与停止点

本轮无Service／emit／flush／close公开接口或成功占位，不启动线程或服务，不写stdout／stderr或真实日志文件，不实现队列、资源准备、轮转、生产装配、Web、审计、Provider或热修改。尚未实现／验证完整并发、规范化槽位与投递资格原子控制、整体内存预算、背压、故障恢复、生命周期、实际投递及持久化；无真实目录选择、G2批准或资源安全证明。用户验收仅覆盖这些纯内存组件。本记录随授权的独立本地提交归档；提交后停止，不安装依赖、推送、合并、部署或开始下一切片。
