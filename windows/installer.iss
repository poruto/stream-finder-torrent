; StreamFinder setup .exe for Windows (Inno Setup 6), built by windows/build-release.ps1:
;   python\   embedded Python with the dependencies
;   app\      the app (VERSION file instead of .git), TorrServer in app\bin
;   whisper\  whisper.cpp server and the fastest model, for AI subtitles
; Installs for the current user (no admin), starts at logon, updates itself by
; downloading and running the newest release's setup (windows/control.py).

#ifndef Version
  #define Version "0.0.0"
#endif
#ifndef Source
  #define Source "..\build\StreamFinder"
#endif

[Setup]
AppId={{6F3C2B8E-4D1A-4E7B-9C5F-2A8D7E1B3C40}
AppName=StreamFinder
AppVersion={#Version}
AppVerName=StreamFinder {#Version}
AppPublisher=PoruTo
AppPublisherURL=https://github.com/poruto/stream-finder-torrent
AppSupportURL=https://github.com/poruto/stream-finder-torrent/issues
DefaultDirName={localappdata}\StreamFinder
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
CloseApplications=no
OutputBaseFilename=StreamFinder-Setup-{#Version}
SetupIconFile={#Source}\app\windows\streamfinder.ico
UninstallDisplayIcon={app}\app\windows\streamfinder.ico
UninstallDisplayName=StreamFinder
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "cs"; MessagesFile: "compiler:Languages\Czech.isl"

[CustomMessages]
en.TmdbTitle=TMDB API key
en.TmdbDescription=The movie catalogue comes from TMDB.
en.TmdbHelp=Free key: sign up at themoviedb.org, then Settings > API > Create > Developer, and copy the "API Key" (not the long Read Access Token). You can also leave it empty and enter it later in StreamFinder: Settings (login admin / admin).
en.TmdbLabel=API key:
en.OpenApp=Open StreamFinder
en.StopApp=Stop StreamFinder
en.Starting=Setting up StreamFinder...
cs.TmdbTitle=TMDB API klíč
cs.TmdbDescription=Katalog filmů a seriálů je z TMDB.
cs.TmdbHelp=Klíč je zdarma: zaregistrujte se na themoviedb.org, pak Nastavení > API > Vytvořit > Developer a zkopírujte "API Key" (ne dlouhý Read Access Token). Pole můžete nechat prázdné a klíč zadat později ve StreamFinderu: Nastavení (přihlášení admin / admin).
cs.TmdbLabel=API klíč:
cs.OpenApp=Otevřít StreamFinder
cs.StopApp=Zastavit StreamFinder
cs.Starting=Nastavuji StreamFinder...

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Files a new version no longer has must not linger (user data in app\.env, cache\ and data\ stays).
Type: filesandordirs; Name: "{app}\app\streamfinder"
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\whisper\build"

[Files]
Source: "{#Source}\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs ignoreversion
Source: "{#Source}\app\*"; DestDir: "{app}\app"; Flags: recursesubdirs ignoreversion
Source: "{#Source}\whisper\build\*"; DestDir: "{app}\whisper\build"; Flags: recursesubdirs ignoreversion
Source: "{#Source}\whisper\models\*"; DestDir: "{app}\whisper\models"; Flags: onlyifdoesntexist

[Icons]
Name: "{autoprograms}\StreamFinder"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\windows\control.py"" open"; WorkingDir: "{app}\app"; IconFilename: "{app}\app\windows\streamfinder.ico"
Name: "{autoprograms}\{cm:StopApp}"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\windows\control.py"" stop"; WorkingDir: "{app}\app"; IconFilename: "{app}\app\windows\streamfinder.ico"
Name: "{autodesktop}\StreamFinder"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\windows\control.py"" open"; WorkingDir: "{app}\app"; IconFilename: "{app}\app\windows\streamfinder.ico"; Tasks: desktopicon
; In the background from every logon (TorrServer, the app, Whisper).
Name: "{userstartup}\StreamFinder"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\windows\control.py"" run"; WorkingDir: "{app}\app"

[Registry]
; The `streamfinder` command in new terminals.
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}\app\windows"; Check: NeedsAddPath(ExpandConstant('{app}\app\windows'))

[Run]
Filename: "{app}\python\python.exe"; Parameters: """{app}\app\windows\control.py"" install ""TMDB_API_KEY={code:TmdbKey}"""; WorkingDir: "{app}\app"; StatusMsg: "{cm:Starting}"; Flags: runhidden
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\windows\control.py"" run"; WorkingDir: "{app}\app"; Flags: nowait
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\windows\control.py"" open"; WorkingDir: "{app}\app"; Description: "{cm:OpenApp}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\python\python.exe"; Parameters: """{app}\app\windows\control.py"" stop"; WorkingDir: "{app}\app"; Flags: runhidden; RunOnceId: "StopStreamFinder"

[Code]
var
  TmdbPage: TInputQueryWizardPage;

function NeedsAddPath(Dir: string): Boolean;
var
  Path: string;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Path) then
    Path := '';
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Path) + ';') = 0;
end;

function HasTmdbKey(): Boolean;
var
  Lines: TArrayOfString;
  I: Integer;
begin
  Result := False;
  if LoadStringsFromFile(ExpandConstant('{app}\app\.env'), Lines) then
    for I := 0 to GetArrayLength(Lines) - 1 do
      if (Pos('TMDB_API_KEY=', Lines[I]) = 1) and (Length(Trim(Lines[I])) > Length('TMDB_API_KEY=')) then
        Result := True;
end;

procedure InitializeWizard();
begin
  TmdbPage := CreateInputQueryPage(wpSelectTasks, CustomMessage('TmdbTitle'),
    CustomMessage('TmdbDescription'), CustomMessage('TmdbHelp'));
  TmdbPage.Add(CustomMessage('TmdbLabel'), False);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  // An update keeps the key it already has.
  Result := (PageID = TmdbPage.ID) and HasTmdbKey();
end;

function TmdbKey(Param: string): string;
begin
  Result := Trim(TmdbPage.Values[0]);
  StringChangeEx(Result, '"', '', True);
end;

// Running files can't be replaced: stop an installed StreamFinder first.
function PrepareToInstall(var NeedsRestart: Boolean): string;
var
  Code: Integer;
  Python, Control: string;
begin
  Python := ExpandConstant('{app}\python\python.exe');
  Control := ExpandConstant('{app}\app\windows\control.py');
  if FileExists(Python) and FileExists(Control) then
    Exec(Python, '"' + Control + '" stop', ExpandConstant('{app}\app'), SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;
