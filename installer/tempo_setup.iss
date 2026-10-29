; Inno Setup Script for TEMPO
; Builds the Windows installer TEMPO_Setup_v1.0.1.exe.

#define MyAppName "TEMPO"
#define MyAppVersion "1.0.1"
#define MyAppPublisher "TEMPO"
#define MyAppURL "https://github.com/tempo-bio/TEMPO"
#define MyAppExeName "TEMPO.exe"

[Setup]
AppId={{D8C8E192-3B48-4FE2-9653-532F99277A81}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
InfoBeforeFile=prerequisites_notice.txt
OutputDir=..\Output
OutputBaseFilename=TEMPO_Setup_v{#MyAppVersion}
SetupIconFile=..\assets\tempo_icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Dist folder contents compiled by PyInstaller
Source: "..\dist\TEMPO\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
