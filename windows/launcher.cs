// StreamFinder.exe of the portable Windows app: starts the tray icon (windows/control.py
// tray), which starts TorrServer, the app and Whisper and opens the browser.
//   StreamFinder.exe               start, open the browser
//   StreamFinder.exe --background  start without opening the browser (Start with Windows)
// Compiled by build-release.ps1 with the C# compiler that comes with Windows:
//   csc /target:winexe /win32icon:streamfinder.ico /r:System.Windows.Forms.dll launcher.cs
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

static class Launcher
{
    [STAThread]
    static void Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(root, "python", "pythonw.exe");
        string control = Path.Combine(root, "app", "windows", "control.py");
        if (!File.Exists(python) || !File.Exists(control))
        {
            MessageBox.Show(
                "Some StreamFinder files are missing. Unzip the whole StreamFinder folder again.",
                "StreamFinder", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        var start = new ProcessStartInfo(python, "\"" + control + "\" tray " + string.Join(" ", args))
        {
            WorkingDirectory = Path.Combine(root, "app"),
            UseShellExecute = false,
        };
        Process.Start(start);
    }
}
