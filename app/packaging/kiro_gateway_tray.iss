; Kiro Gateway Tray - Windows Installer
; Usage (from make_dist.py, paths are absolute):
;   ISCC /DAppVersion=X.Y.Z /DDistDir=<abs>\dist\KiroGatewayTray /DOutputDir=<abs>\release kiro_gateway_tray.iss

#ifndef AppVersion
#define AppVersion "0.1.0"
#endif
#ifndef DistDir
#define DistDir "..\dist\KiroGatewayTray"
#endif
#ifndef OutputDir
#define OutputDir "..\release"
#endif

[Setup]
; Fixed identity so future AppName changes are still recognised as upgrades of
; the same product (and uninstall stays clean). NEVER change this value.
AppId={{8A76AFC5-28C9-450B-94DF-1DE5BECB6EAF}
AppName=Kiro Gateway Tray
AppVersion={#AppVersion}
AppVerName=Kiro Gateway Tray {#AppVersion}
AppPublisher=kiro-gateway-deploy
AppPublisherURL=https://github.com/zhujunsan/kiro-gateway-deploy
AppSupportURL=https://github.com/zhujunsan/kiro-gateway-deploy/issues
AppCopyright=Copyright (C) kiro-gateway-deploy
; Stamp the generated setup.exe's file properties (right-click → Details).
VersionInfoVersion={#AppVersion}
VersionInfoCompany=kiro-gateway-deploy
VersionInfoProductName=Kiro Gateway Tray
DefaultDirName={autopf}\KiroGatewayTray
DefaultGroupName=Kiro Gateway Tray
DisableProgramGroupPage=yes
UninstallDisplayName=Kiro Gateway Tray
UninstallDisplayIcon={app}\KiroGatewayTray.exe
OutputDir={#OutputDir}
OutputBaseFilename=KiroGatewayTray-{#AppVersion}-windows-amd64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Default to a per-user / per-machine choice instead of forcing admin. The
; wizard shows a page letting the user pick; admin installs to Program Files,
; non-admin installs under the user's profile.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
; amd64 desktop app: only allow 64-bit Windows, and require Windows 10+.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Don't let the Restart Manager close the running app automatically; we ask the
; user first in PrepareToInstall and only then terminate it.
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "languages\ChineseSimplified.isl"

[CustomMessages]
english.AppRunningPrompt=Kiro Gateway Tray is currently running and must be closed before setup can continue.%n%nDo you want Setup to close it now?%n(Choosing No will cancel the installation.)
english.AppRunningCancelled=Setup was cancelled because Kiro Gateway Tray is still running.
chinesesimplified.AppRunningPrompt=Kiro Gateway Tray 正在运行，需要先关闭才能继续安装。%n%n是否现在由安装程序关闭它？%n（选择“否”将取消本次安装。）
chinesesimplified.AppRunningCancelled=因 Kiro Gateway Tray 仍在运行，安装已取消。

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Kiro Gateway Tray"; Filename: "{app}\KiroGatewayTray.exe"
Name: "{commondesktop}\Kiro Gateway Tray"; Filename: "{app}\KiroGatewayTray.exe"; Tasks: desktopicon; Check: IsAdminInstallMode
Name: "{userdesktop}\Kiro Gateway Tray"; Filename: "{app}\KiroGatewayTray.exe"; Tasks: desktopicon; Check: not IsAdminInstallMode

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Run]
Filename: "{app}\KiroGatewayTray.exe"; Description: "{cm:LaunchProgram,Kiro Gateway Tray}"; Flags: nowait postinstall skipifsilent

[Code]
function IsProcessRunning(ImageName: String): Boolean;
var
  ResultCode: Integer;
begin
  { tasklist filtered by image name returns errorlevel 0 and prints the row
    when a match exists; we redirect output and rely on the exit code via a
    cmd wrapper that 'find's the image name. }
  Result := Exec(
    ExpandConstant('{cmd}'),
    '/C tasklist /FI "IMAGENAME eq ' + ImageName + '" /NH | find /I "' + ImageName + '" > nul',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) and (ResultCode = 0);
end;

procedure KillProcessByImageName(ImageName: String);
var
  ResultCode: Integer;
begin
  Exec(
    ExpandConstant('{sys}\taskkill.exe'),
    '/IM "' + ImageName + '" /T /F',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  AppRunning: Boolean;
  HelperRunning: Boolean;
begin
  Result := '';
  AppRunning := IsProcessRunning('KiroGatewayTray.exe');
  HelperRunning := IsProcessRunning('cloudflared.exe');

  if not (AppRunning or HelperRunning) then
    exit;

  if SuppressibleMsgBox(
       ExpandConstant('{cm:AppRunningPrompt}'),
       mbConfirmation, MB_YESNO, IDYES) = IDYES then
  begin
    if AppRunning then
      KillProcessByImageName('KiroGatewayTray.exe');
    if HelperRunning then
      KillProcessByImageName('cloudflared.exe');
  end
  else
    Result := ExpandConstant('{cm:AppRunningCancelled}');
end;
