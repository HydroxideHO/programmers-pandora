#define MyAppName "Programmer's Pandora"
#define MyAppVersion "1.2.0"
#define MyAppExe "pandora_home.exe"

[Setup]
AppId={{6C688C6E-B476-4138-A3FB-51ACA42E1717}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
SetupIconFile=Logo\pandora.ico
SourceDir=..
OutputDir=installer\Output
OutputBaseFilename=ProgrammersPandora-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; Compiled, self-contained tool executables (built via installer\build.bat,
; which runs PyInstaller against ProgrammersPandora.spec before this compiles) -
; no Python installation needed on the target PC.
Source: "installer\dist\ProgrammersPandora\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Raw .py sources are also shipped so pandora_home.exe's launcher can read each
; tool's docstring for its title/description - see discover_scripts() in
; pandora_home.py. They're not executed directly; the .exe above is.
Source: "*.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "Logo\*"; DestDir: "{app}\Logo"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; AppUserModelID must match what pandora_theme.set_window_icon() sets for the
; running launcher, or a pinned taskbar shortcut and its open window show up
; as two separate taskbar buttons.
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; WorkingDir: "{app}"; IconFilename: "{app}\Logo\pandora.ico"; AppUserModelID: "HydroxideHO.ProgrammersPandora.pandora_home"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; WorkingDir: "{app}"; Tasks: desktopicon; IconFilename: "{app}\Logo\pandora.ico"; AppUserModelID: "HydroxideHO.ProgrammersPandora.pandora_home"
