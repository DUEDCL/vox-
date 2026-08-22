# Vox 项目接手与改名迁移

状态：REVIEW

## 目标
将当前 Vox 项目完整接手并改名为 `vox`，清理工作路径中的临时/外部产物，保留所有现有业务改动，接入 GitHub `DUEDCL/vox-` 仓库（用户提供地址；当前远程为空）。

## 允许修改范围
- 项目源码、测试、文档、配置和桌面元数据中属于项目改名的内容
- `.ai/tasks/**`、`.ai/handoffs/**`、`.ai/reviews/**`
- 当前工作区明确的临时/外部未跟踪产物：仅归档到工作区外备份目录，不直接删除
- Git remote、分支和新建远程仓库

## 禁止修改范围
- `enrollment/`、`.env`、凭据、声纹数据、原始音频和模型权重
- `contracts/voice-events.schema.json` 的字节内容与版本
- 数据库结构、核心依赖、部署安全边界
- 现有用户未提交业务修改的语义
- Git 历史的破坏性操作：reset、clean、强制推送

## 验收标准
1. 工作区根目录不再混入已识别的 pytest/design/export 外部产物；归档可追溯。
2. 项目品牌、包名、桌面产品名、可执行文件名、文档和项目自有环境变量统一为 `vox`；EvoX 作为可选 Agent 后端名称保留。
3. Python 测试、前端构建、Rust 检查通过。
4. 新仓库 `DUEDCL/vox` 已接入并推送迁移分支；不覆盖远程已有内容。
5. 所有验证等级如实记录，REAL-AGENT/REAL-EVOX/REAL-WIN 不因改名而虚报。

## 验证命令
```powershell
.\\.venv\\Scripts\\python.exe -m pytest tests -q
Push-Location desktop; npm run build; Pop-Location
Push-Location desktop/src-tauri; cargo check; Pop-Location
```
