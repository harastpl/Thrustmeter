[Setup]
AppName=Scientech 2 DoF
AppVersion=1.0
DefaultDirName={pf}\Scientech 2 DoF
DefaultGroupName=Scientech
OutputDir=installer
OutputBaseFilename=setup
SetupIconFile=icon.ico

[Files]
Source: "C:\Users\USER\Desktop\2_DoF\dist\Scientech 2 DoF.exe"; DestDir: "{app}"

[Icons]
Name: "{group}\Scientech 2 DoF"; Filename: "{app}\Scientech 2 DoF.exe"
Name: "{commondesktop}\Scientech 2 DoF"; Filename: "{app}\Scientech 2 DoF.exe"