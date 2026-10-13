// Adapted from LiveCaptions-Translator LiveCaptionsHandler.cs.
// Copyright (c) 2024 SakiRinn and other contributors. Apache-2.0.
// Ativia modifications (2026-09-16): scoped attachment, ownership, bounded polling,
// JSON pipe, cancellation, parent watchdog; never kill a LiveCaptions process.
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Automation;

public class CaptionBridge {
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr FindWindow(string cls, string title);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out int pid);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr h);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int command);
    [DllImport("user32.dll")] static extern bool PostMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
    static volatile bool stop = false;
    static volatile string command = "";
    static JavaScriptSerializer json = new JavaScriptSerializer();
    static void Send(object message) { Console.WriteLine(json.Serialize(message)); Console.Out.Flush(); }
    static bool Valid(IntPtr h, int pid) { int found; GetWindowThreadProcessId(h, out found); return IsWindow(h) && found == pid; }
    [MTAThread] public static int Main(string[] args) {
        Console.OutputEncoding = new UTF8Encoding(false);
        int parentId = int.Parse(args[0]);
        bool closeOwned = args.Length > 1 && args[1] == "yes";
        Process parent = Process.GetProcessById(parentId);
        Thread watchdog = new Thread(() => { while (!stop) { try { if (parent.HasExited) stop = true; } catch { stop = true; } Thread.Sleep(250); } });
        watchdog.IsBackground = true; watchdog.Start();
        Thread input = new Thread(() => { string line; while ((line=Console.ReadLine()) != null) { if (line == "stop") {stop=true; break;} command=line; } stop=true; });
        input.IsBackground=true; input.Start();
        IntPtr hwnd=IntPtr.Zero; int pid=0; bool owned=false, minimizedByUs=false, ready=false;
        Process launched=null;
        try {
            hwnd=FindWindow("LiveCaptionsDesktopWindow",null);
            if (hwnd==IntPtr.Zero) {
                // An existing process may still be loading. Never launch over it.
                Process[] existing=Process.GetProcessesByName("LiveCaptions");
                bool already=existing.Length>0;
                foreach (Process p in existing) p.Dispose();
                if (!already) {
                    launched=Process.Start(new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),"LiveCaptions.exe")) { UseShellExecute=false });
                }
                for (int i=0;i<120 && hwnd==IntPtr.Zero && !stop;i++) { Thread.Sleep(125); hwnd=FindWindow("LiveCaptionsDesktopWindow",null); }
            }
            if (stop) return 0;
            if (hwnd==IntPtr.Zero) { Send(new {kind="error", code="window_missing"}); return 2; }
            GetWindowThreadProcessId(hwnd,out pid);
            using (Process p=Process.GetProcessById(pid)) { if (p.ProcessName!="LiveCaptions") throw new InvalidOperationException(); }
            owned=launched!=null && launched.Id==pid;
            Send(new {kind="window", hwnd=hwnd.ToInt64(), pid=pid, owned=owned, minimized=IsIconic(hwnd)});
            AutomationElement window=AutomationElement.FromHandle(hwnd);
            AutomationElement text=null; AutomationElement idleText=null; DateTime nextSearch=DateTime.MinValue;
            PropertyCondition condition=new PropertyCondition(AutomationElement.AutomationIdProperty,"CaptionsTextBlock");
            string previous=null; int missing=0, ticks=0;
            while (!stop && Valid(hwnd,pid)) {
                string cmd=command; command="";
                if (cmd=="show" && IsIconic(hwnd)) { ShowWindow(hwnd,4); minimizedByUs=false; }
                if (cmd=="hide" && !IsIconic(hwnd)) { ShowWindow(hwnd,6); minimizedByUs=true; }
                try {
                    if (text==null && DateTime.UtcNow>=nextSearch) { text=window.FindFirst(TreeScope.Descendants,condition); nextSearch=DateTime.UtcNow.AddMilliseconds(500); }
                    if (text!=null) {
                        string value=text.Current.Name ?? "";
                        if (!ready) { Send(new {kind="ready"}); ready=true; }
                        if (value!=previous) { Send(new {kind="snapshot",text=value}); previous=value; }
                        else if (++ticks%4==0) Send(new {kind="tick"});
                        missing=0;
                    } else { if(idleText==null) idleText=window.FindFirst(TreeScope.Descendants,new PropertyCondition(AutomationElement.AutomationIdProperty,"ReadyToCaptionTextBlock")); var idle=idleText; if(idle!=null) { if(!ready) {Send(new {kind="ready",description=idle.Current.Name}); Send(new {kind="snapshot",text=""}); ready=true;} Send(new {kind="tick"}); missing=0; } else if(++missing>=120) {Send(new {kind="error",code="setup_required"}); return 3;} }
                } catch (ElementNotAvailableException) { text=null; idleText=null; if (++missing>=120) throw; }
                Thread.Sleep(text==null && ready ? 250 : 125);
            }
            if (!stop) Send(new {kind="error",code="window_closed"});
            text=null; window=null;
            return 0;
        } catch { Send(new {kind="error",code="automation_failed"}); return 4; }
        finally {
            // Do not close first-use consent/language setup. Let the user finish it.
            if (Valid(hwnd,pid)) {
                if (minimizedByUs) ShowWindow(hwnd,4);
                if (owned && ready && closeOwned) PostMessage(hwnd,0x0010,IntPtr.Zero,IntPtr.Zero);
            }
            stop=true;
            if (launched!=null) launched.Dispose();
            parent.Dispose();
        }
    }
}

