@echo off
rem Builds winuae64.exe (FullRelease x64) with Visual Studio 2022 into linkuae\build\out\
rem   build.cmd [extra MSBuild arguments]
rem MSBuild is found with vswhere; to use another one, set MSBUILD_EXE to its full path first.
setlocal

set "MSBUILD=%MSBUILD_EXE%"
rem Outside the block below: the parenthesis in %ProgramFiles(x86)% would end it early.
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not defined MSBUILD (
  if not exist "%VSWHERE%" (
    echo vswhere not found: install Visual Studio 2022 with the C++ desktop workload,
    echo or set MSBUILD_EXE to the full path of MSBuild.exe.
    exit /b 1
  )
  for /f "usebackq delims=" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find MSBuild\**\Bin\MSBuild.exe`) do set "MSBUILD=%%i"
)
if not defined MSBUILD (
  echo MSBuild with the C++ tools not found: install the C++ desktop workload of Visual Studio 2022,
  echo or set MSBUILD_EXE to the full path of MSBuild.exe.
  exit /b 1
)
if not exist "%MSBUILD%" (
  echo Not found: "%MSBUILD%"
  exit /b 1
)

set "ROOT=%~dp0..\.."
echo MSBuild: %MSBUILD%
rem The doubled backslash after "out" keeps the closing quote from being swallowed.
"%MSBUILD%" "%ROOT%\od-win32\winuae_msvc15\winuae_msvc.vcxproj" /m /nologo /v:minimal ^
  /p:Configuration=FullRelease /p:Platform=x64 /p:PlatformToolset=v143 ^
  "/p:OutDir=%~dp0out\\" "/p:ForceImportAfterCppTargets=%~dp0local.props" %*
if errorlevel 1 exit /b 1
echo built %~dp0out\winuae64.exe
