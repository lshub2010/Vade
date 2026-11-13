# Real-window capture for the Miuix spike (Windows only).
#
# Launches the Desktop app from the compiled classpath, pins the Skiko window
# top-most, crops by its DWM extended frame bounds (GetWindowRect keeps an 8 px
# invisible resize border that would pull neighbours into the crop) and saves a
# PNG. One SunAwtFrame per process is what we see; an OverlayDialog draws inside
# that same window (a bottom sheet via MiuixPopupHost inside Miuix's Scaffold), it
# never adds a window of its own — pass -ClickX/-ClickY to open it before the shot.
#
# Prerequisite (from spike/miuix):
#   gradle :composeApp:desktopMainClasses :composeApp:printRuntimeClasspath -q | tail -1 > cp.txt
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools/capture-window.ps1 `
#     -Java "$env:JAVA_HOME/bin/java.exe" -ClassPathFile cp.txt `
#     -Section card -OutFile shots/miuix-window-card.png -NoDesktopShot
param(
  [Parameter(Mandatory=$true)][string]$Java,
  [Parameter(Mandatory=$true)][string]$ClassPathFile,
  [Parameter(Mandatory=$true)][string]$Section,
  [Parameter(Mandatory=$true)][string]$OutFile,
  [int]$WaitSec = 45,
  [int]$SettleMs = 2400,
  [int]$ClickX = -1,
  [int]$ClickY = -1,
  [int]$AfterClickMs = 1600,
  [switch]$NoDesktopShot
)

Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public struct WRECT6 { public int Left; public int Top; public int Right; public int Bottom; }
public class WCap6 {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr hWnd, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out WRECT6 r);
  [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr hWnd, int attr, out WRECT6 r, int size);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint flags, int dx, int dy, uint data, UIntPtr extra);
  [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);

  // CopyFromScreen grabs whatever is on top and SetForegroundWindow is often refused
  // for a process spawned in the background, so pin the window above the rest.
  public static void MakeTopMost(IntPtr h) { SetWindowPos(h, (IntPtr)(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040); }
  public static void DropTopMost(IntPtr h) { SetWindowPos(h, (IntPtr)(-2), 0, 0, 0, 0, 0x0001 | 0x0002); }
  public static void Click(int x, int y) {
    SetCursorPos(x, y);
    System.Threading.Thread.Sleep(300);
    mouse_event(0x0002, 0, 0, 0, UIntPtr.Zero);
    System.Threading.Thread.Sleep(90);
    mouse_event(0x0004, 0, 0, 0, UIntPtr.Zero);
  }
  public static void MoveCursor(int x, int y) { SetCursorPos(x, y); }
  public static List<IntPtr> WindowsOf(uint pid) {
    var list = new List<IntPtr>();
    EnumWindows(delegate(IntPtr h, IntPtr l) {
      uint p; GetWindowThreadProcessId(h, out p);
      if (p == pid && IsWindowVisible(h)) list.Add(h);
      return true;
    }, IntPtr.Zero);
    return list;
  }
  public static string Title(IntPtr h) { var sb = new StringBuilder(512); GetWindowTextW(h, sb, 512); return sb.ToString(); }
  public static string Cls(IntPtr h) { var sb = new StringBuilder(512); GetClassNameW(h, sb, 512); return sb.ToString(); }
  public static bool Visible(IntPtr h) {
    WRECT6 d; if (DwmGetWindowAttribute(h, 9, out d, 16) == 0 && (d.Right - d.Left) > 100) { return true; }
    return false;
  }
}
"@
$DWMWA_EXTENDED_FRAME_BOUNDS = 9

$cp = (Get-Content -Raw -LiteralPath $ClassPathFile).Trim()
if (-not $cp) { Write-Output 'NO_CLASSPATH'; exit 3 }

$p = Start-Process -FilePath $Java -ArgumentList @('-Djava.awt.headless=false', '-cp', $cp, 'vade.spike.miuix.MainKt', "--section=$Section") -PassThru -WindowStyle Hidden
Write-Output "launched pid=$($p.Id) section=$Section"

$main = [IntPtr]::Zero
$deadline = (Get-Date).AddSeconds($WaitSec)
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Milliseconds 400
  if ($p.HasExited) { Write-Output 'PROCESS_EXITED'; exit 4 }
  foreach ($h in [WCap6]::WindowsOf([uint32]$p.Id)) {
    if (-not ([WCap6]::Cls($h)).StartsWith('SunAwt')) { continue }
    $r = New-Object WRECT6; [void][WCap6]::GetWindowRect($h, [ref]$r)
    if (($r.Right - $r.Left) -gt 300 -and ($r.Bottom - $r.Top) -gt 300) { $main = $h; break }
  }
  if ($main -ne [IntPtr]::Zero) { break }
}
if ($main -eq [IntPtr]::Zero) {
  Write-Output 'NO_WINDOW'
  Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
  exit 2
}

[WCap6]::DropTopMost($main)
[void][WCap6]::BringWindowToTop($main)
[void][WCap6]::SetForegroundWindow($main)
[WCap6]::MakeTopMost($main)
Start-Sleep -Milliseconds $SettleMs

# Visible frame per window (GetWindowRect carries the 8 px invisible resize border
# on Win10/11, which would pull neighbours into the crop).
$frames = @()
foreach ($h in [WCap6]::WindowsOf([uint32]$p.Id)) {
  $cls = [WCap6]::Cls($h)
  $raw = New-Object WRECT6; [void][WCap6]::GetWindowRect($h, [ref]$raw)
  $vis = New-Object WRECT6
  $hr = [WCap6]::DwmGetWindowAttribute($h, $DWMWA_EXTENDED_FRAME_BOUNDS, [ref]$vis, 16)
  $use = if ($hr -eq 0 -and ($vis.Right - $vis.Left) -gt 60) { $vis } else { $raw }
  $w = $use.Right - $use.Left; $ht = $use.Bottom - $use.Top
  if (-not $cls.StartsWith('SunAwt')) { Write-Output ("  skip non-awt  hwnd={0} {1}x{2} '{3}' cls={4}" -f $h, $w, $ht, [WCap6]::Title($h), $cls); continue }
  if ($w -lt 60 -or $ht -lt 60) { Write-Output ("  skip tiny     hwnd={0} {1}x{2} '{3}'" -f $h, $w, $ht, [WCap6]::Title($h)); continue }
  Write-Output ("  window        hwnd={0} {1},{2} {3}x{4} '{5}' cls={6}" -f $h, $use.Left, $use.Top, $w, $ht, [WCap6]::Title($h), $cls)
  $frames += ,$use
}
if ($frames.Count -eq 0) { Write-Output 'NO_FRAME'; Stop-Process -Id $p.Id -Force; exit 5 }

$useL = [int]($frames | ForEach-Object { $_.Left } | Measure-Object -Minimum).Minimum
$useT = [int]($frames | ForEach-Object { $_.Top } | Measure-Object -Minimum).Minimum
$useR = [int]($frames | ForEach-Object { $_.Right } | Measure-Object -Maximum).Maximum
$useB = [int]($frames | ForEach-Object { $_.Bottom } | Measure-Object -Maximum).Maximum
$w = $useR - $useL; $h = $useB - $useT
Write-Output "capture rect = $useL,$useT ${w}x${h} (awt windows=$($frames.Count))"

if ($ClickX -ge 0 -and $ClickY -ge 0) {
  $cx = $useL + $ClickX; $cy = $useT + $ClickY
  Write-Output "click at window+($ClickX,$ClickY) -> screen ($cx,$cy)"
  [WCap6]::Click($cx, $cy)
  Start-Sleep -Milliseconds $AfterClickMs
  [WCap6]::MoveCursor(6, 1060)
  Start-Sleep -Milliseconds 350
}

$bmp = New-Object System.Drawing.Bitmap($w, $h)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($useL, $useT, 0, 0, (New-Object System.Drawing.Size($w, $h)))
$dir = Split-Path -Parent $OutFile
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
$bmp.Save($OutFile, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Output "saved $OutFile ${w}x${h} $((Get-Item $OutFile).Length) bytes"

if (-not $NoDesktopShot) {
  $screenBmp = New-Object System.Drawing.Bitmap(1920, 1080)
  $g2 = [System.Drawing.Graphics]::FromImage($screenBmp)
  $g2.CopyFromScreen(0, 0, 0, 0, (New-Object System.Drawing.Size(1920, 1080)))
  $fullPath = [System.IO.Path]::ChangeExtension($OutFile, '.desktop.png')
  $screenBmp.Save($fullPath, [System.Drawing.Imaging.ImageFormat]::Png)
  $g2.Dispose(); $screenBmp.Dispose()
  Write-Output "saved $fullPath (full desktop 1920x1080)"
}

[WCap6]::DropTopMost($main)
Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
Write-Output "killed pid=$($p.Id)"
