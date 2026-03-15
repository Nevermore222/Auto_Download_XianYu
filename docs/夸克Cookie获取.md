# 夸克网盘 Cookie 获取方法

本系统通过浏览器登录态（Cookie）向夸克网盘上传文件并生成分享链接，需要您自行获取 Cookie 并填入配置文件。

## 步骤

1. **登录夸克网盘**  
   使用浏览器打开：https://pan.quark.cn ，登录您的夸克账号。

2. **打开开发者工具**  
   - Windows：按 `F12` 或 `Ctrl + Shift + I`  
   - Mac：`Cmd + Option + I`  
   切换到 **Network（网络）** 面板。

3. **触发一次请求**  
   在网盘页面随便点击一下（如进入某个文件夹），让浏览器产生请求。

4. **复制 Cookie**  
   - 在 Network 里点任意一个请求（域名一般为 `pan.quark.cn` 或 `drive.quark.cn`）  
   - 在请求头里找到 **Request Headers** → **Cookie**  
   - 将整行 Cookie 全部复制（通常是一长串 `key1=value1; key2=value2; ...`）

5. **填入配置**  
   打开项目中的 `config/config.yaml`，在夸克相关配置里找到 `cookie` 或 `quark_cookie`，把复制的内容粘贴进去，注意用引号包起来，例如：

   ```yaml
   quark:
     cookie: "你的完整Cookie字符串"
     # 如有其他选项（如上传目录）也在此填写
   ```

## 安全提醒

- Cookie 等同于登录凭证，**不要**分享给他人或提交到公开代码仓库。
- 若怀疑泄露，请立即在夸克网页端修改密码并重新登录，再重新获取 Cookie。
- 本机测试时可将 Cookie 只写在本地配置文件，并确保 `config.yaml` 已加入 `.gitignore`。

## Cookie 失效

若上传时报错「未登录」或 401，多半是 Cookie 过期，请重新登录夸克网盘并按上述步骤重新复制 Cookie 并更新配置。
