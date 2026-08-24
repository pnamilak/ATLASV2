#define MyAppName "AWS Trusted Login & Access Service"
#define MyAppVersion "2.0.0"
#define MyAppPublisher "Philips-SRCOps"
#define MyAppURL "https://github.com/philips-internal/ATLAS"
#define MyPrimaryExe "ATLAS.exe"

[Setup]
AppId={{B6FDF3D7-7C9D-4D6F-9D5E-1F8A9B4C3D21}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}

DefaultDirName={pf}\{#MyAppName}
UsePreviousAppDir=yes
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes

PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog

ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os

Compression=lzma
SolidCompression=yes
WizardStyle=modern

SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#MyPrimaryExe}

OutputDir=Output
OutputBaseFilename=ATLAS-Setup-{#MyAppVersion}

CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "dist\ATLAS\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

Source: "prereqs\VC_redist.x64.exe"; Flags: ignoreversion dontcopy
Source: "prereqs\WebView2RuntimeInstallerX64.exe"; Flags: ignoreversion dontcopy

#if FileExists("prereqs\AWSCLIV2.msi")
Source: "prereqs\AWSCLIV2.msi"; Flags: ignoreversion dontcopy
#endif

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyPrimaryExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyPrimaryExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyPrimaryExe}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
const
  WM_SETTINGCHANGE = $001A;

function SendMessageTimeout(hWnd: Integer; Msg: Integer; wParam: Integer; lParam: string;
  fuFlags: Integer; uTimeout: Integer; var lpdwResult: Integer): Integer;
  external 'SendMessageTimeoutW@user32.dll stdcall';

procedure BroadcastEnvChange();
var
  Res: Integer;
begin
  Res := 0;
  SendMessageTimeout($FFFF, WM_SETTINGCHANGE, 0, 'Environment', 0, 5000, Res);
end;

function GetDataDir(): string;
begin
  Result := ExpandConstant('{localappdata}\ATLAS');
end;

procedure EnsureDir(const DirPath: string);
begin
  if not DirExists(DirPath) then
    ForceDirectories(DirPath);
end;

function IsVCRuntimeInstalled(): Boolean;
var
  Installed: Cardinal;
begin
  Installed := 0;

  if RegQueryDWordValue(HKLM,
      'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
      'Installed', Installed) then
  begin
    Result := Installed = 1;
    Exit;
  end;

  Installed := 0;

  if RegQueryDWordValue(HKLM,
      'SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
      'Installed', Installed) then
  begin
    Result := Installed = 1;
    Exit;
  end;

  Result := False;
end;

function HasWebView2Runtime(): Boolean;
begin
  Result :=
    DirExists(ExpandConstant('{pf}\Microsoft\EdgeWebView\Application')) or
    DirExists(ExpandConstant('{pf32}\Microsoft\EdgeWebView\Application')) or
    DirExists(ExpandConstant('{localappdata}\Microsoft\EdgeWebView\Application')) or
    RegKeyExists(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients') or
    RegKeyExists(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients') or
    RegKeyExists(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients');
end;

function AwsExePathCandidate1(): string;
begin
  Result := ExpandConstant('{pf}\Amazon\AWSCLIV2\aws.exe');
end;

function AwsExePathCandidate2(): string;
begin
  Result := ExpandConstant('{pf32}\Amazon\AWSCLIV2\aws.exe');
end;

function IsAwsCliInstalled(): Boolean;
begin
  Result :=
    FileExists(AwsExePathCandidate1()) or
    FileExists(AwsExePathCandidate2()) or
    RegKeyExists(HKLM, 'SOFTWARE\Amazon\AWSCLI') or
    RegKeyExists(HKLM, 'SOFTWARE\WOW6432Node\Amazon\AWSCLI');
end;

procedure SetMachineEnvVar(const Name, Value: string);
begin
  if not RegWriteStringValue(HKLM,
    'SYSTEM\CurrentControlSet\Control\Session Manager\Environment',
    Name, Value) then
  begin
    Log('WARN: Failed to set machine env var ' + Name);
  end
  else
  begin
    Log('Set env var: ' + Name + '=' + Value);
  end;
end;

procedure EnsureAwsCliPathEnvVar();
var
  AwsPath: string;
begin
  AwsPath := '';

  if FileExists(AwsExePathCandidate1()) then
    AwsPath := AwsExePathCandidate1()
  else if FileExists(AwsExePathCandidate2()) then
    AwsPath := AwsExePathCandidate2();

  if AwsPath <> '' then
    SetMachineEnvVar('AWS_CLI_PATH', AwsPath)
  else
    Log('AWS CLI installed but aws.exe not found in standard locations.');
end;

procedure InstallPrereqs();
var
  NeedVC, NeedWV, NeedAWS: Boolean;
  VCExe, WVExe, AwsMsi: string;
  ResultCode: Integer;
begin
  EnsureDir(GetDataDir() + '\logs');

  NeedVC := not IsVCRuntimeInstalled();
  NeedWV := not HasWebView2Runtime();
  NeedAWS := not IsAwsCliInstalled();

  Log('=== Prereq detection ===');
  Log('need_vc=' + IntToStr(Ord(NeedVC)));
  Log('need_webview2=' + IntToStr(Ord(NeedWV)));
  Log('need_awscli=' + IntToStr(Ord(NeedAWS)));

  if NeedVC then
  begin
    ExtractTemporaryFile('VC_redist.x64.exe');
    VCExe := ExpandConstant('{tmp}\VC_redist.x64.exe');
    Exec(VCExe, '/install /quiet /norestart', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Log('vc_exit_code=' + IntToStr(ResultCode));
  end;

  if NeedWV then
  begin
    ExtractTemporaryFile('WebView2RuntimeInstallerX64.exe');
    WVExe := ExpandConstant('{tmp}\WebView2RuntimeInstallerX64.exe');
    Exec(WVExe, '/silent /install /norestart', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Log('webview2_exit_code=' + IntToStr(ResultCode));
  end;

  if NeedAWS then
  begin
    try
      ExtractTemporaryFile('AWSCLIV2.msi');
      AwsMsi := ExpandConstant('{tmp}\AWSCLIV2.msi');
      Exec(ExpandConstant('{sys}\msiexec.exe'),
        '/i "' + AwsMsi + '" /qn /norestart',
        '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      Log('awscli_exit_code=' + IntToStr(ResultCode));
    except
      Log('AWS CLI MSI not bundled or extract failed.');
    end;
  end;

  EnsureAwsCliPathEnvVar();
  BroadcastEnvChange();
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    InstallPrereqs();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
  Answer: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    DataDir := GetDataDir();

    if DirExists(DataDir) then
    begin
      Answer := MsgBox(
        'Do you also want to REMOVE user data at:' + #13#10 +
        DataDir + #13#10#13#10 +
        'YES = full reset (delete DB/catalog/logs)' + #13#10 +
        'NO  = keep data (recommended)',
        mbConfirmation, MB_YESNO);

      if Answer = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;