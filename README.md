# My IPA sources

## 订阅地址

- [LiveContainer 订阅（source.json）](https://raw.githubusercontent.com/beckyeeky/lc-source/main/source.json)
- [SideStore 独立订阅（sidestore.json）](https://raw.githubusercontent.com/beckyeeky/lc-source/main/sidestore.json)

`sources.json` 是生成脚本的项目配置，**不能作为订阅导入**。`scripts/sidestore-cache.json` 是 IPA 元数据缓存，同样不是订阅源。

## 两份订阅的区别

- `source.json` 保持原有地址、应用、测试版／修复版别名和版本历史，不因这次 SideStore 适配而改变。
- `sidestore.json` 从同一批下载链接独立生成，当前只选正式版／原包名条目。LiveContainer 配置中带 `bundleIdSuffix` 的测试版、修复分支不混入 SideStore；未改 IPA 本体的别名包名无法通过 SideStore 的真实包名校验。
- 每个 SideStore 条目保留最新一个已验证的版本，使用 IPA 内 `Info.plist` 的真实包名、版本号、构建号和最低系统版本，不把 Release 标签冒充应用版本。
- 自动读取主程序及扩展的签名权限键和隐私用途声明，计算下载 IPA 的 SHA-256。解析失败时保留该应用上一次的 SideStore 条目；新应用无法验证则暂不收录。不会关闭 SideStore 的安全检查。
- 有效元数据按下载 URL 缓存，重复更新无需重下所有 IPA。新 URL 才重新检查；同 URL/大小的不可变发布资产由缓存保留原哈希。

## 更新

现有 `Update IPA source` 工作流每 6 小时及手动触发时依次生成两份订阅。ThreadsPulse 自动搬运发布新 Release 后也触发该工作流，因此会同步更新两份源。两者共用 IPA 下载资产，不使用 R2。

生成命令：

```sh
python3 gen_source.py sources.json source.json
python3 scripts/gen_sidestore.py
python3 -m unittest discover -s tests -v
```

SideStore 字段依据其开源代码的 [StoreApp 解析](https://github.com/SideStore/SideStore/blob/develop/AltStore/Core/Model/StoreApp.swift)、[AppVersion 解析](https://github.com/SideStore/SideStore/blob/develop/AltStore/Core/Model/AppVersion.swift)和[安装校验](https://github.com/SideStore/SideStore/blob/develop/SideStore/Core/Operations/PipelineOperations/VerifyAppOperation.swift)。自动校验不等于真机安装测试，也不保证修改版应用安全；扩展的 App ID 配额、证书能力与系统限制仍需用户确认。
