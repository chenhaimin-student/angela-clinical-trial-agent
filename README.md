# Angela Clinical Trial Agent

[English](README.en.md)

Angela 是一个面向个人技术作品集的临床试验信息检索与编排项目。它把公开临床试验数据的采集、结构化查询、证据约束、请求编排、Browser Worker 和 Hermes MCP 集成拆分为可测试的模块。

> 本项目只处理公开临床试验信息，用于信息检索和研究辅助演示。它不是医疗器械，不提供诊断、治疗建议或最终入组结论。任何真实研究参与决定都必须由研究机构和合格专业人员完成。

## 核心模块

- `angela_orchestration/`：隐私拦截、意图路由、患者条件结构化、证据过滤、并发调度和请求状态机。
- `cde-data/`：公开 CDE 数据的解析、SQLite 模式、增量同步和只读查询服务。仓库不附带正式数据库或原始网页归档。
- `cde-demo/`：本地演示客户端，以及隔离浏览器访问的 TypeScript Browser Worker。
- `hermes-cde-tool/`：Angela 编写的 MCP Server、Hermes 适配器和版本相关补丁；Hermes 本体不在本仓库中。
- `services/session-compactor/`：可抢占的长会话压缩协调逻辑，只保存协调元数据，不应记录消息正文。
- `quality_eval/`：离线契约、检索证据和回答边界评估工具。

## 安全边界

- 只允许输入去标识化的检索条件；姓名、证件号、电话、病历号等直接标识符会被拒绝。
- 本仓库不包含生产 SQLite、备份、日志、浏览器 Profile、聊天记录、患者数据或凭据。
- 模型路由是可选能力；确定性隐私规则和证据校验位于模型调用之前。
- Browser Worker 默认只绑定 `127.0.0.1`，配置文件必须由使用者在本地创建。
- 公开结果是候选信息和证据摘要，不是最终资格判断。

## 快速开始

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
```

复制 `.env.example` 为本地 `.env`，并从公开来源自行准备数据：

```bash
python cde-data/scripts/init_db.py --database .runtime/cde_trials.sqlite3
```

不要把正式数据库、原始 HTML 或运行时目录加入 Git。

## 测试

```bash
python -m unittest discover -s hermes-cde-tool/tests -p "test_*.py"
python -m unittest discover -s cde-data/tests -p "test_*.py"
python -m unittest discover -s tests -p "test_*.py"
python -m unittest discover -s services/session-compactor/tests -p "test_*.py"
```

Browser Worker：

```bash
cd cde-demo/cde-browser-worker
corepack enable
pnpm install --frozen-lockfile
pnpm build
pnpm test
```

完整说明见 [架构](docs/ARCHITECTURE.md)、[部署](docs/DEPLOYMENT.md)、[测试](docs/TESTING.md) 和 [隐私](docs/PRIVACY.md)。

## 许可证与第三方软件

Angela 自有代码按 [Portfolio Source-Available License 1.0](LICENSE) 提供，不是 OSI 认可的开源许可证。Hermes Agent 0.19.0 是 MIT 许可的第三方软件；本仓库只保留 Angela 编写的集成与补丁，不包含 Hermes 完整源码。详情见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
