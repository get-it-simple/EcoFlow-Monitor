Unicode true
ManifestDPIAware true
ManifestDPIAwareness PerMonitorV2
!ifndef APP_VERSION
  !error "APP_VERSION is required"
!endif
!ifndef APP_EXE
  !error "APP_EXE is required"
!endif
!ifndef OUTPUT_FILE
  !error "OUTPUT_FILE is required"
!endif
!ifdef INSTALL_SCOPE
  !if "${INSTALL_SCOPE}" != "machine"
    !error "Only machine installation is supported"
  !endif
!endif
!ifndef SIGNED_BUILD
  !define SIGNED_BUILD 0
!endif
!define MACHINE_INSTALL
!define PRODUCT_EXE "EcoFlowMonitor.exe"
!define PRODUCT_ID "GetItSimple.EcoFlowMonitor"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_ID}"
!define PRODUCT_URL "https://github.com/get-it-simple/EcoFlow-Monitor"
Name "EcoFlow Monitor"
OutFile "${OUTPUT_FILE}"
InstallDir "$PROGRAMFILES64\EcoFlowMonitor"
RequestExecutionLevel user
BrandingText "Get it Simple"
SetCompressor /SOLID zlib
CRCCheck force
ShowInstDetails show
ShowUninstDetails show
VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey /LANG=1033 "ProductName" "EcoFlow Monitor"
VIAddVersionKey /LANG=1033 "CompanyName" "Get it Simple"
VIAddVersionKey /LANG=1033 "FileDescription" "EcoFlow Monitor Installer"
VIAddVersionKey /LANG=1033 "FileVersion" "${APP_VERSION}.0"
VIAddVersionKey /LANG=1033 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "LegalCopyright" "Copyright (c) 2026 Get it Simple"
!ifdef UNINSTALL_SIGN_COMMAND
  !uninstfinalize '${UNINSTALL_SIGN_COMMAND}' = 0
!endif
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "WinVer.nsh"
!include "x64.nsh"
!include "nsDialogs.nsh"
!define MUI_ICON "..\ecoflow_monitor\ui\assets\app.ico"
!define MUI_UNICON "..\ecoflow_monitor\ui\assets\app.ico"
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_FUNCTION FinishRun
!define MUI_FINISHPAGE_RUN_NOTCHECKED
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipProgressPages
!define MUI_PAGE_CUSTOMFUNCTION_SHOW ApplySystemTheme
!insertmacro MUI_PAGE_WELCOME
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipProgressPages
!define MUI_PAGE_CUSTOMFUNCTION_SHOW ApplySystemTheme
!insertmacro MUI_PAGE_LICENSE "..\LICENSE"
!define MUI_PAGE_CUSTOMFUNCTION_SHOW ApplySystemTheme
!insertmacro MUI_PAGE_INSTFILES
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipProgressPages
!define MUI_PAGE_CUSTOMFUNCTION_SHOW ApplySystemTheme
!insertmacro MUI_PAGE_FINISH
!define MUI_PAGE_CUSTOMFUNCTION_SHOW un.ApplySystemTheme
!insertmacro MUI_UNPAGE_CONFIRM
!define MUI_PAGE_CUSTOMFUNCTION_SHOW un.ApplySystemTheme
!insertmacro MUI_UNPAGE_INSTFILES
!define MUI_PAGE_CUSTOMFUNCTION_SHOW un.ApplySystemTheme
!insertmacro MUI_UNPAGE_FINISH
!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_LANGUAGE "Ukrainian"
LangString StopFailed ${LANG_ENGLISH} "Close the installed EcoFlow Monitor in every Windows session and retry. No files were changed."
LangString StopFailed ${LANG_UKRAINIAN} "Закрийте встановлений EcoFlow Monitor в усіх сеансах Windows і повторіть спробу. Файли не змінено."
LangString InstallFailed ${LANG_ENGLISH} "Installation failed or administrator approval was cancelled."
LangString InstallFailed ${LANG_UKRAINIAN} "Встановлення не вдалося або дозвіл адміністратора скасовано."
LangString InvalidPath ${LANG_ENGLISH} "EcoFlow Monitor requires Windows 10 1809 or newer, x64, and must be installed in Program Files\EcoFlowMonitor."
LangString InvalidPath ${LANG_UKRAINIAN} "EcoFlow Monitor потребує Windows 10 1809 або новішої, x64, та встановлюється лише в Program Files\EcoFlowMonitor."
LangString InvalidExisting ${LANG_ENGLISH} "The existing installation could not be verified. Remove it manually before continuing."
LangString InvalidExisting ${LANG_UKRAINIAN} "Не вдалося перевірити наявне встановлення. Видаліть його вручну перед продовженням."
Var ElevatedPhase
Var ElevateArguments
Var ElevationExitCode
Var SilentProgress
Var ProcessStatus
Var ProcessTarget
Var ExistingDir

!macro ShowSetupError MESSAGE
  StrCmp $SilentProgress 1 +2
  MessageBox MB_OK|MB_ICONSTOP "${MESSAGE}" /SD IDOK
!macroend

Function SkipProgressPages
  StrCmp $SilentProgress 1 0 done
  Abort
done:
FunctionEnd

!macro Initialization UN
Function ${UN}.onInit
  SetRegView 64
  StrCpy $LANGUAGE ${LANG_ENGLISH}
  System::Call 'kernel32::GetUserDefaultUILanguage() i.r0'
  IntCmp $0 ${LANG_UKRAINIAN} 0 +2 +2
  StrCpy $LANGUAGE ${LANG_UKRAINIAN}
  ${IfNot} ${RunningX64}
    Goto invalid
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    Goto invalid
  ${EndIf}
  ReadRegStr $0 HKLM "Software\Microsoft\Windows NT\CurrentVersion" "CurrentBuildNumber"
  IntCmp $0 17763 supported invalid supported
supported:
  StrCpy $INSTDIR "$PROGRAMFILES64\EcoFlowMonitor"
  ClearErrors
  ${GetOptions} $CMDLINE "/D=" $0
  IfErrors path_valid
  System::Call 'kernel32::lstrcmpiW(w r0, w "$INSTDIR") i.r1'
  StrCmp $1 0 path_valid invalid
path_valid:
  StrCpy $ElevatedPhase 0
  StrCpy $SilentProgress 0
  ClearErrors
  ${GetOptions} $CMDLINE "/SILENTWITHPROGRESS" $0
  IfErrors elevation
  StrCpy $SilentProgress 1
  SetAutoClose true
elevation:
  ClearErrors
  ${GetOptions} $CMDLINE "/ELEVATED" $0
  IfErrors done
  UserInfo::GetAccountType
  Pop $0
  StrCmp $0 "Admin" 0 invalid
  StrCpy $ElevatedPhase 1
  Goto done
invalid:
  MessageBox MB_OK|MB_ICONSTOP "$(InvalidPath)" /SD IDOK
  SetErrorLevel 4
  Quit
done:
FunctionEnd
!macroend
!insertmacro Initialization ""
!insertmacro Initialization "un"

!macro StopInstalled UN
Function ${UN}StopInstalled
  StrCpy $ProcessTarget "$INSTDIR\${PRODUCT_EXE}"
  Call ${UN}InspectExactProcess
  StrCmp $ProcessStatus 0 done
  StrCmp $ProcessStatus 1 0 failed
  nsExec::ExecToStack /TIMEOUT=15000 '"$ProcessTarget" --shutdown-for-update'
  Pop $0
  Pop $1
  StrCmp $0 0 0 failed
  StrCpy $9 100
wait_exit:
  Call ${UN}InspectExactProcess
  StrCmp $ProcessStatus 0 done
  StrCmp $ProcessStatus 3 failed
  Sleep 100
  IntOp $9 $9 - 1
  IntCmp $9 0 failed wait_exit wait_exit
failed:
  !insertmacro ShowSetupError "$(StopFailed)"
  SetErrorLevel 2
  Quit
done:
FunctionEnd
!macroend
!insertmacro StopInstalled ""
!insertmacro StopInstalled "un."

Function FinishRun
  Exec '"$INSTDIR\${PRODUCT_EXE}"'
FunctionEnd

Function ValidateExisting
  ReadRegStr $ExistingDir HKLM "${UNINSTALL_KEY}" "InstallLocation"
  StrCmp $ExistingDir "" unregistered
  System::Call 'kernel32::lstrcmpiW(w "$ExistingDir", w "$INSTDIR") i.r0'
  StrCmp $0 0 check_marker invalid
unregistered:
  IfFileExists "$INSTDIR\${PRODUCT_EXE}" check_marker done
check_marker:
  IfFileExists "$INSTDIR\.ecoflow-monitor-installed" 0 invalid
  System::Call 'kernel32::GetFileAttributesW(w "$INSTDIR") i.r0'
  IntOp $0 $0 & 0x400
  StrCmp $0 0 done invalid
invalid:
  !insertmacro ShowSetupError "$(InvalidExisting)"
  SetErrorLevel 3
  Quit
done:
FunctionEnd

Section "Install"
  Call ValidateExisting
  StrCmp $ElevatedPhase 1 files
  Call StopInstalled
  StrCpy $ElevateArguments "/S /ELEVATED"
  Call ElevateAndWait
  StrCmp $ElevationExitCode 0 done
  !insertmacro ShowSetupError "$(InstallFailed)"
  SetErrorLevel $ElevationExitCode
  StrCmp $SilentProgress 1 0 +2
  Quit
  Abort
files:
  Call InspectForWrite
  SetShellVarContext all
  SetOutPath "$INSTDIR"
  ClearErrors
  File /oname=EcoFlowMonitor.exe "${APP_EXE}"
  IfErrors failed
  File /oname=LICENSE "..\LICENSE"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  IfErrors failed
  FileOpen $0 "$INSTDIR\.ecoflow-monitor-installed" w
  FileWrite $0 "version=${APP_VERSION}$\r$\nscope=machine$\r$\nsigned=${SIGNED_BUILD}$\r$\n"
  FileClose $0
  CreateDirectory "$SMPROGRAMS\EcoFlow Monitor"
  CreateShortcut "$SMPROGRAMS\EcoFlow Monitor\EcoFlow Monitor.lnk" "$INSTDIR\${PRODUCT_EXE}"
  CreateShortcut "$SMPROGRAMS\EcoFlow Monitor\Uninstall EcoFlow Monitor.lnk" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayName" "EcoFlow Monitor"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "Publisher" "Get it Simple"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\${PRODUCT_EXE}"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "URLInfoAbout" "${PRODUCT_URL}"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegStr HKLM "${UNINSTALL_KEY}" "QuietUninstallString" '$\"$INSTDIR\Uninstall.exe$\" /S'
  WriteRegDWORD HKLM "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINSTALL_KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  WriteRegDWORD HKLM "${UNINSTALL_KEY}" "EstimatedSize" $0
  Goto done
failed:
  SetErrorLevel 3
  Abort
done:
SectionEnd

Function InspectForWrite
  StrCpy $ProcessTarget "$INSTDIR\${PRODUCT_EXE}"
  Call InspectExactProcess
  StrCmp $ProcessStatus 0 done
  SetErrorLevel 2
  Abort
done:
FunctionEnd

Section "Uninstall"
  IfFileExists "$INSTDIR\.ecoflow-monitor-installed" 0 invalid
  System::Call 'kernel32::GetFileAttributesW(w "$INSTDIR") i.r0'
  IntOp $0 $0 & 0x400
  StrCmp $0 0 validated invalid
invalid:
  SetErrorLevel 3
  Abort
validated:
  StrCmp $ElevatedPhase 1 files
  Call un.StopInstalled
  StrCpy $ElevateArguments "/S /ELEVATED"
  Call un.ElevateAndWait
  SetErrorLevel $ElevationExitCode
  StrCmp $ElevationExitCode 0 0 done
  ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "EcoFlowMonitor"
  StrCmp $0 '$\"$INSTDIR\${PRODUCT_EXE}$\" --autostart' 0 done
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "EcoFlowMonitor"
  Goto done
files:
  StrCpy $ProcessTarget "$INSTDIR\${PRODUCT_EXE}"
  Call un.InspectExactProcess
  StrCmp $ProcessStatus 0 0 invalid
  SetShellVarContext all
  ClearErrors
  Delete "$INSTDIR\${PRODUCT_EXE}"
  IfErrors invalid
  Delete "$SMPROGRAMS\EcoFlow Monitor\EcoFlow Monitor.lnk"
  Delete "$SMPROGRAMS\EcoFlow Monitor\Uninstall EcoFlow Monitor.lnk"
  RMDir "$SMPROGRAMS\EcoFlow Monitor"
  DeleteRegKey HKLM "${UNINSTALL_KEY}"
  Delete "$INSTDIR\.ecoflow-monitor-installed"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
done:
SectionEnd
!include "theme.nsh"
!include "process_control.nsh"
!include "elevation.nsh"
