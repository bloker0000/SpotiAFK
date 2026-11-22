# tucks the web player away, -Show brings it back to log in
param([switch]$Show, [int]$WaitSeconds = 0)
. (Join-Path $PSScriptRoot 'common.ps1')

# a type can only be added once per process
if (-not ('PlayerWindow' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class PlayerWindow {
    const int GWL_EXSTYLE = -20;
    const long WS_EX_TRANSPARENT = 0x20;  // clicks go through it
    const long WS_EX_TOOLWINDOW = 0x80;  // no taskbar button, not in alt-tab
    const long WS_EX_LAYERED = 0x80000;
    const long WS_EX_APPWINDOW = 0x40000;
    const long TUCKED = WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED;
    const uint LWA_ALPHA = 0x2;
    const int SW_HIDE = 0;
    const int SW_SHOWNOACTIVATE = 4;
    const int SW_RESTORE = 9;
    const uint SWP_NOZORDER = 0x4;
    const uint SWP_NOACTIVATE = 0x10;
    static readonly IntPtr HWND_BOTTOM = new IntPtr(1);
    const int SM_XVIRTUALSCREEN = 76;
    const int SM_YVIRTUALSCREEN = 77;
    const int OFF_SCREEN_MARGIN = 100;
    const int TUCKED_WIDTH = 800;
    const int TUCKED_HEIGHT = 600;
    const int SHOWN_AT = 100;
    const int SHOWN_WIDTH = 1200;
    const int SHOWN_HEIGHT = 900;

    [StructLayout(LayoutKind.Sequential)]
    struct Rect { public int Left, Top, Right, Bottom; }

    delegate bool EnumProc(IntPtr window, IntPtr parameter);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc callback, IntPtr parameter);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    [DllImport("user32.dll")] static extern int GetWindowTextLength(IntPtr window);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int GetClassName(IntPtr window, StringBuilder name, int size);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr window, int command);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr window, out Rect rect);
    [DllImport("user32.dll")] static extern int GetSystemMetrics(int index);
    [DllImport("user32.dll")]
    static extern bool SetLayeredWindowAttributes(IntPtr window, uint key, byte alpha, uint flags);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")]
    static extern IntPtr GetWindowLongPtr(IntPtr window, int index);
    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")]
    static extern IntPtr SetWindowLongPtr(IntPtr window, int index, IntPtr value);
    [DllImport("user32.dll")]
    static extern bool SetWindowPos(IntPtr window, IntPtr after, int left, int top, int width, int height, uint flags);

    // chrome's helper windows have no title
    public static List<IntPtr> Find(HashSet<uint> processIds) {
        var found = new List<IntPtr>();
        EnumWindows((window, _) => {
            uint processId;
            GetWindowThreadProcessId(window, out processId);
            var name = new StringBuilder(64);
            GetClassName(window, name, name.Capacity);
            if (processIds.Contains(processId) && name.ToString() == "Chrome_WidgetWin_1"
                && GetWindowTextLength(window) > 0) {
                found.Add(window);
            }
            return true;
        }, IntPtr.Zero);
        return found;
    }

    public static bool IsTucked(IntPtr window) {
        long style = GetWindowLongPtr(window, GWL_EXSTYLE).ToInt64();
        Rect rect;
        GetWindowRect(window, out rect);
        return (style & TUCKED) == TUCKED && rect.Right <= GetSystemMetrics(SM_XVIRTUALSCREEN);
    }

    // never hidden or minimized, chrome pauses those pages and the player stalls
    public static void Tuck(IntPtr window) {
        // the taskbar only rereads the style while the window is hidden
        ShowWindow(window, SW_HIDE);
        SetExStyle(window, TUCKED, WS_EX_APPWINDOW);
        // see-through too, windows can pull it back on screen
        SetLayeredWindowAttributes(window, 0, 0, LWA_ALPHA);
        ShowWindow(window, SW_SHOWNOACTIVATE);
        // after showing, chrome resets its bounds on show
        int left = GetSystemMetrics(SM_XVIRTUALSCREEN) - TUCKED_WIDTH - OFF_SCREEN_MARGIN;
        SetWindowPos(window, HWND_BOTTOM, left, GetSystemMetrics(SM_YVIRTUALSCREEN),
            TUCKED_WIDTH, TUCKED_HEIGHT, SWP_NOACTIVATE);
    }

    public static void Bring(IntPtr window) {
        ShowWindow(window, SW_HIDE);
        SetExStyle(window, 0, TUCKED);
        ShowWindow(window, SW_RESTORE);
        SetWindowPos(window, IntPtr.Zero, SHOWN_AT, SHOWN_AT, SHOWN_WIDTH, SHOWN_HEIGHT, SWP_NOZORDER);
        SetForegroundWindow(window);
    }

    static void SetExStyle(IntPtr window, long add, long remove) {
        long style = GetWindowLongPtr(window, GWL_EXSTYLE).ToInt64();
        SetWindowLongPtr(window, GWL_EXSTYLE, new IntPtr((style | add) & ~remove));
    }
}
'@
}

if ($Show) {
    New-Item -ItemType File -Force $playerShownFlag | Out-Null
} else {
    Remove-Item $playerShownFlag -ErrorAction SilentlyContinue
}

$deadline = (Get-Date).AddSeconds($WaitSeconds)
do {
    $processIds = New-Object 'System.Collections.Generic.HashSet[uint32]'
    Get-PlayerProcess | ForEach-Object { [void]$processIds.Add([uint32]$_.ProcessId) }
    $windows = [PlayerWindow]::Find($processIds)
    foreach ($window in $windows) {
        if ($Show) {
            [PlayerWindow]::Bring($window)
        } elseif (-not [PlayerWindow]::IsTucked($window)) {
            [PlayerWindow]::Tuck($window)
        }
    }
    if ($windows.Count) {
        return
    }
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $deadline)
