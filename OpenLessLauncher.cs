using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Threading;
using Microsoft.Win32;

// Windowless lifecycle owner. The official OpenLess executable is unmodified.
class OpenLessLauncher {
    static readonly string Root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
    static readonly string App = Path.Combine(Directory.GetParent(Root).FullName, "OpenLess", "openless.exe");
    static readonly string Self = Process.GetCurrentProcess().MainModule.FileName;
    const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    static void Log(string text) {
        Directory.CreateDirectory(Path.Combine(Root,"data"));
        File.AppendAllText(Path.Combine(Root,"data","lifecycle.log"), DateTime.Now.ToString("s")+" "+text+Environment.NewLine);
    }
    static void Manage(string action) {
        var start = new ProcessStartInfo(Path.Combine(Root,"runtime","Scripts","pythonw.exe"),
            "\""+Path.Combine(Root,"manage.py")+"\" "+action);
        start.UseShellExecute=false; start.CreateNoWindow=true; start.WorkingDirectory=Root;
        start.EnvironmentVariables["DOUBAO_BRIDGE_OWNER_PID"]=Process.GetCurrentProcess().Id.ToString();
        using(var process=Process.Start(start)) {
            if(!process.WaitForExit(10000)) { process.Kill(); throw new Exception("Service control timed out"); }
            if(process.ExitCode!=0) throw new Exception("Service control failed");
        }
    }
    static bool AppRunning() {
        foreach(var process in Process.GetProcessesByName("openless")) {
            using(process) {
                try { if(String.Equals(process.MainModule.FileName,App,StringComparison.OrdinalIgnoreCase)) return true; }
                catch(System.ComponentModel.Win32Exception) { /* Other users' processes are not ours. */ }
                catch(InvalidOperationException) { }
            }
        }
        return false;
    }
    static void RouteAutostart() {
        using(var key=Registry.CurrentUser.OpenSubKey(RunKey,true)) {
            if(key==null) return;
            var value=key.GetValue("OpenLess") as string;
            if(value==null) return; // Respect disabling the app's own autostart toggle.
            if(value.Trim().Trim('"').Equals(App,StringComparison.OrdinalIgnoreCase)) {
                key.SetValue("OpenLess","\""+Self+"\"",RegistryValueKind.String);
                Log("Routed enabled OpenLess autostart through launcher");
            }
        }
    }
    static void StartApp(string[] args) {
        var start=new ProcessStartInfo(App);
        start.WorkingDirectory=Path.GetDirectoryName(App);
        start.UseShellExecute=true;
        // OpenLess normal launch has no arguments. Preserve its supported autostart flag.
        foreach(var arg in args) if(arg=="--autostart" || arg=="--minimized") start.Arguments+=" "+arg;
        using(var process=Process.Start(start)) { }
    }
    [STAThread]
    static int Main(string[] args) {
        bool created;
        using(var mutex=new Mutex(true,@"Local\DoubaoAsrBridge.OpenLessLifecycle",out created)) {
            if(!created) { StartApp(args); return 0; }
            try {
                Log("Lifecycle started");
                Manage("stop");
                Manage("start");
                bool ready=false;
                for(int i=0;i<50;i++) {
                    try {
                        var request=WebRequest.Create("http://127.0.0.1:18765/health");
                        request.Proxy=null; request.Timeout=300;
                        using(var response=request.GetResponse()) { ready=true; break; }
                    } catch(WebException) { Thread.Sleep(100); }
                }
                if(!ready) throw new Exception("Bridge not ready");
                RouteAutostart();
                StartApp(args);
                for(int i=0;i<40 && !AppRunning();i++) Thread.Sleep(250);
                if(!AppRunning()) throw new Exception("OpenLess did not start");
                Log("OpenLess running; bridge ready");
                while(AppRunning()) { RouteAutostart(); Thread.Sleep(500); }
                RouteAutostart();
                Log("OpenLess exited");
                return 0;
            } catch(Exception error) {
                Log("Lifecycle failed: "+error.GetType().Name);
                return 1;
            } finally {
                try { Manage("stop"); Log("Bridge stopped; lifecycle exiting"); } catch(Exception) { Log("Service cleanup failed"); }
                mutex.ReleaseMutex();
            }
        }
    }
}
