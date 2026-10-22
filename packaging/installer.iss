; ============================================================================
; LightDB Studio — Inno Setup 安装包脚本（P6 发布工程）
;
; 本机未安装 Inno Setup，因此**此脚本未在本机用 ISCC 实际编译过**；
; 它由 scripts/build_installer.py 在检测到 ISCC.exe 后以 /D 宏方式调用：
;
;   ISCC.exe /DAppVersion=4.0.0 ^
;            /DSourceDir="<repo>\dist\studio\LightDB-Studio" ^
;            /DOutputDir="<repo>\dist\installer" ^
;            /DOutputBaseFilename="LightDB-Studio-Setup-4.0.0" ^
;            packaging\installer.iss
;
; 可复现构建（工具缺失时 build_installer.py 也会生成同样的命令行）：
;   1) 安装 Inno Setup 6+（https://jrsoftware.org/isdl.php，或
;      winget install --id JRSoftware.InnoSetup -e）
;   2) python scripts/build_installer.py --tool auto --smoke
;   3) 产物：dist/installer/LightDB-Studio-Setup-<version>.exe
;
; 运行期静默安装/卸载（供冒烟与 IT 部署）：
;   LightDB-Studio-Setup-4.0.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
;        /DIR="C:\tmp\LightDBStudio"          ; 自定义安装目录（可选安装目录）
;   "C:\tmp\LightDBStudio\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
;
; 特点：开始菜单快捷方式 + 卸载项（Inno 自动生成 unins000.exe 与
; HKCU/HKLM ...\Uninstall 注册项）+ 可选桌面图标 + 可选安装目录。
; 安装/卸载均不需要管理员权限（PrivilegesRequired=lowest；可被管理员覆盖）。
; ============================================================================

#ifndef AppVersion
  #define AppVersion "4.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\studio\LightDB-Studio"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\installer"
#endif
#ifndef OutputBaseFilename
  #define OutputBaseFilename "LightDB-Studio-Setup-" + AppVersion
#endif

[Setup]
; AppId 固定（升级/卸载按它匹配）—— 不要每次构建变化，否则会装出多份
AppId={{8E0D2B6E-6E42-4A7B-9F5B-1B0B4C9B0A61}
AppName=LightDB Studio
AppVersion={#AppVersion}
AppVerName=LightDB Studio {#AppVersion}
AppPublisher=LightDB Team
AppPublisherURL=https://github.com/lightdb/studio
AppSupportURL=https://github.com/lightdb/studio
DefaultDirName={autopf}\LightDB Studio
DefaultGroupName=LightDB Studio
DisableProgramGroupPage=yes
; 最低权限：普通用户可装到自己的目录（静默 /DIR 冒烟也不需要管理员）
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\LightDB-Studio.exe
UninstallDisplayName=LightDB Studio {#AppVersion}
; 最低支持的 Windows 版本（与 PyInstaller 产物一致：Win10+）
MinVersion=10.0
; 不需要联网/不需要重启
DisableReadyPage=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; 桌面图标可选（默认不选）；开始菜单快捷方式始终创建（除非 /NOICONS）
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
    GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
; onedir 产物（dist/studio/LightDB-Studio）整目录复制；_internal 一并递归复制
Source: "{#SourceDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
; 保留文档（若有）——缺失不报错
Source: "{#SourceDir}\..\..\README.md"; DestDir: "{app}"; \
    Flags: ignoreversion skipifsourcedoesntexist

[Icons]
; 开始菜单：主程序 + 卸载入口（Inno 的标准卸载项也会出现在“应用和功能”）
Name: "{group}\LightDB Studio"; Filename: "{app}\LightDB-Studio.exe"; \
    WorkingDir: "{app}"
Name: "{group}\Uninstall LightDB Studio"; Filename: "{uninstallexe}"
Name: "{autodesktop}\LightDB Studio"; Filename: "{app}\LightDB-Studio.exe"; \
    WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; 安装完成后可选启动（静默安装不弹）
Filename: "{app}\LightDB-Studio.exe"; Description: "Launch LightDB Studio"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; 卸载前尽量结束正在运行的 Studio（失败不阻塞卸载）
Filename: "{cmd}"; Parameters: "/c taskkill /IM LightDB-Studio.exe /F"; \
    Flags: runhidden; RunOnceId: "StopStudio"

[UninstallDelete]
; PyInstaller/Studio 运行期可能写入安装目录内缓存/日志；卸载时清理
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\cache"
Type: files; Name: "{app}\*.log"
