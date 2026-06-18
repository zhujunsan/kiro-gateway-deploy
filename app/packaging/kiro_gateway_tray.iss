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
AppName=Kiro Gateway Tray
AppVersion={#AppVersion}
AppPublisher=kiro-gateway-deploy
AppPublisherURL=https://github.com/zhujunsan/kiro-gateway-deploy
AppSupportURL=https://github.com/zhujunsan/kiro-gateway-deploy/issues
DefaultDirName={autopf}\KiroGatewayTray
DefaultGroupName=Kiro Gateway Tray
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=KiroGatewayTray-{#AppVersion}-windows-amd64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Kiro Gateway Tray"; Filename: "{app}\KiroGatewayTray.exe"
Name: "{commondesktop}\Kiro Gateway Tray"; Filename: "{app}\KiroGatewayTray.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Run]
Filename: "{app}\KiroGatewayTray.exe"; Description: "{cm:LaunchProgram,Kiro Gateway Tray}"; Flags: nowait postinstall skipifsilent
