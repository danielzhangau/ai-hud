# AI-HUD Config - 使用说明

本 U 盘内含 macOS 和 Windows 两个版本的 Config 工具。请按你的电脑系统选择。

---

## macOS 用户

第一次打开（重要 -- Gatekeeper 安全机制）：

1. 解压 `AI-HUD Config.zip`（或打开 `For macOS/` 子目录里的版本）
2. 把 `AI-HUD Config.app` 拖到 `应用程序` 文件夹（或桌面）
3. **右键单击** `AI-HUD Config.app`，选择 **"打开"**（不是双击）
4. 弹出警告："无法验证开发者..."，点击 **"打开"** 按钮
5. 完成 -- 浏览器会自动打开配置页面

之后每次使用：**直接双击** `AI-HUD Config.app` 即可。

---

## Windows 用户

第一次打开：

1. 打开 `For Windows/` 文件夹
2. 把 `AI-HUD Config (Windows).zip` 解压到任意位置（桌面 / 文档均可）
3. 进入解压出的 `AI-HUD Config` 文件夹
4. **双击** `Run AI-HUD Config.bat`
5. 如果 Windows SmartScreen 弹 "Windows 已保护你的电脑"：
   - 点击左上角的 **"更多信息"**
   - 点击底部出现的 **"仍要运行"** 按钮
6. 浏览器自动打开配置页面

之后每次使用：双击 `Run AI-HUD Config.bat` 即可。

---

## 日常使用流程（mac 与 Windows 通用）

```
1. 用 USB 线把 AI-HUD 设备连接到电脑
2. 等 5-10 秒（设备启动）
3. 双击 Config 启动器
4. 浏览器自动打开 → 可以修改设置
```

设置会**实时保存**到设备并立即生效。

---

## 常见问题

### 弹出 "未检测到 AI-HUD 设备"

- 检查 USB 线是否插好（必须是**数据线**，纯供电线不行）
- 等设备完全启动（约 30 秒）后再点
- 试一个不同的 USB 口（避免 USB Hub）

### 设备已连接但配置服务无响应

- 等 30 秒后再试
- 还不行：拔下 USB、等 5 秒、重新插上，再试

### Windows: "无法运行此应用 (.bat 被阻止)"

- 右键 `Run AI-HUD Config.bat` → "更多选项" → "以管理员身份运行"
- 仅第一次需要这样；之后双击即可

### Firmware 升级

目前 Firmware 升级仅支持 macOS。如需升级 Firmware：
- macOS 用户：按住设备 BOOT 按钮 + 重插 USB → 双击 Config.app
- Windows 用户：联系开发者，或借一台 mac 完成

普通的代码 / 数据库升级在 Windows 上完全支持，**不需要** firmware 操作。

---

如有其它问题，请联系开发者。
