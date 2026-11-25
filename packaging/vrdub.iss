; ============================================================================
; VRDub 无言势 · 安装包脚本（Inno Setup 6）
;
; 由 tools\build_installer.py 调用，版本号从 vrdub\version.py 传进来：
;   ISCC.exe /DAppVer=0.9.0 /DAppVerFull=0.9.0-dev /DProjRoot=D:\vrc无言势 vrdub.iss
;
; 设计取舍：
; - 默认装到 {autopf}\VRDub（管理员 = Program Files，普通用户 = LocalAppData\Programs），
;   因为程序本体只读，配置/日志/模型缓存全在 %LOCALAPPDATA%\VRDub，UAC 不弹也能用。
; - 模型（252 MB）直接塞进安装包：朋友那边不联网、不用 pip、不用配 PYTHONPATH。
; - 卸载时问一句"要不要连配置和日志一起删"，默认不删（重装不用重新配）。
; ============================================================================

#ifndef AppVer
  #define AppVer "0.0.0"
#endif
#ifndef AppVerFull
  #define AppVerFull "0.0.0-dev"
#endif
#ifndef ProjRoot
  #define ProjRoot ".."
#endif

#define AppName "VRDub 无言势"
#define AppExe "VRDub.exe"
#define AppId "{{3F2C9B7E-5C41-4D2A-9E3B-6F1D8A0C4B52}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVer}
AppVerName={#AppName} {#AppVerFull}
AppPublisher=VRDub
AppPublisherURL=https://github.com/Doctor-182/diyic
AppSupportURL=https://github.com/Doctor-182/diyic/issues
AppUpdatesURL=https://github.com/Doctor-182/diyic/releases
VersionInfoVersion={#AppVer}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVer}
VersionInfoDescription={#AppName} 安装程序
VersionInfoCompany=VRDub
DefaultDirName={autopf}\VRDub
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#ProjRoot}\dist\installer
OutputBaseFilename=VRDub-{#AppVerFull}-setup
SetupIconFile={#ProjRoot}\packaging\vrdub.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
AllowNoIcons=yes
ShowLanguageDialog=auto

[Languages]
Name: "cn"; MessagesFile: "{#ProjRoot}\packaging\lang\ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
Source: "{#ProjRoot}\dist\exe\VRDub\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#ProjRoot}\packaging\lang\安装说明.txt"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
Filename: "{#ProjRoot}\packaging\lang\安装说明.txt"; Description: "看安装说明"; Flags: shellexec nowait postinstall skipifsilent unchecked

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"

[Code]
var
  KeepUserData: Boolean;

function InitializeUninstall(): Boolean;
var
  Home: String;
begin
  Result := True;
  Home := ExpandConstant('{localappdata}\VRDub');
  if DirExists(Home) then
    KeepUserData := MsgBox('要连配置、日志和缓存一起删掉吗？' + #13#10 + #13#10 +
      '选"是"= 删干净（下次装要重新填 key、重选设备）' + #13#10 +
      '选"否"= 保留 ' + Home + '（重装不用重配）', mbConfirmation, MB_YESNO) = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Home: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if not KeepUserData then
    begin
      Home := ExpandConstant('{localappdata}\VRDub');
      DelTree(Home, True, True, True);
    end;
  end;
end;
