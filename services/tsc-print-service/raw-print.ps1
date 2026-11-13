param([string]$DataPath,[string]$PrinterName,[switch]$CompileOnly)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class TscRawResult {
    public bool success;
    public string outcome = "FAILED";
    public string message;
    public string stage;
    public int errorCode, jobId, bytesRequested, bytesWritten;
}
public static class TscRawPrinter {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public class DocInfo {
        [MarshalAs(UnmanagedType.LPWStr)] public string name = "Factory SN label";
        [MarshalAs(UnmanagedType.LPWStr)] public string output = null;
        [MarshalAs(UnmanagedType.LPWStr)] public string type = "RAW";
    }
    [DllImport("winspool.drv",EntryPoint="OpenPrinterW",CharSet=CharSet.Unicode,SetLastError=true)]
    static extern bool OpenPrinter(string name,out IntPtr handle,IntPtr defaults);
    [DllImport("winspool.drv",EntryPoint="StartDocPrinterW",CharSet=CharSet.Unicode,SetLastError=true)]
    static extern int StartDocPrinter(IntPtr handle,int level,[In] DocInfo info);
    [DllImport("winspool.drv",SetLastError=true)] static extern bool StartPagePrinter(IntPtr h);
    [DllImport("winspool.drv",SetLastError=true)] static extern bool WritePrinter(IntPtr h,IntPtr data,int size,out int written);
    [DllImport("winspool.drv",SetLastError=true)] static extern bool EndPagePrinter(IntPtr h);
    [DllImport("winspool.drv",SetLastError=true)] static extern bool EndDocPrinter(IntPtr h);
    [DllImport("winspool.drv",SetLastError=true)] static extern bool AbortPrinter(IntPtr h);
    [DllImport("winspool.drv",SetLastError=true)] static extern bool ClosePrinter(IntPtr h);
    public static TscRawResult Send(string name,byte[] data) {
        var r=new TscRawResult(); r.bytesRequested=data.Length;
        IntPtr h=IntPtr.Zero,buffer=IntPtr.Zero;
        bool doc=false,complete=false,writeAttempted=false;
        try {
            r.stage="OpenPrinter";
            if(!OpenPrinter(name,out h,IntPtr.Zero)) throw new System.ComponentModel.Win32Exception();
            r.stage="StartDocPrinter";
            r.jobId=StartDocPrinter(h,1,new DocInfo());
            if(r.jobId==0) throw new System.ComponentModel.Win32Exception();
            doc=true; r.stage="StartPagePrinter";
            if(!StartPagePrinter(h)) throw new System.ComponentModel.Win32Exception();
            buffer=Marshal.AllocCoTaskMem(data.Length); Marshal.Copy(data,0,buffer,data.Length);
            r.stage="WritePrinter";
            while(r.bytesWritten<data.Length) {
                int written; writeAttempted=true;
                bool ok=WritePrinter(h,IntPtr.Add(buffer,r.bytesWritten),data.Length-r.bytesWritten,out written);
                r.bytesWritten+=written;
                if(!ok || written<=0) throw new System.ComponentModel.Win32Exception();
            }
            r.stage="EndPagePrinter";
            if(!EndPagePrinter(h)) throw new System.ComponentModel.Win32Exception();
            r.stage="EndDocPrinter";
            if(!EndDocPrinter(h)) throw new System.ComponentModel.Win32Exception();
            complete=true; r.success=true; r.outcome="SUBMITTED";r.stage="Submitted";
            r.message="Windows RAW job submitted; physical output not confirmed";
        } catch(Exception e) {
            var win=e as System.ComponentModel.Win32Exception;
            r.errorCode=win==null ? 0 : win.NativeErrorCode;
            r.message=r.stage+": "+e.Message; r.outcome=writeAttempted ? "UNKNOWN" : "FAILED";
        } finally {
            if(buffer!=IntPtr.Zero) Marshal.FreeCoTaskMem(buffer);
            if(h!=IntPtr.Zero) { if(doc && !complete) AbortPrinter(h); ClosePrinter(h); }
        }
        return r;
    }
}
'@
if($CompileOnly) { @{ok=$true;hardwareUsed=$false} | ConvertTo-Json -Compress; exit 0 }
if(-not $DataPath -or -not $PrinterName) { throw 'DataPath and PrinterName are required' }
$bytes=[IO.File]::ReadAllBytes($DataPath)
$result=[TscRawPrinter]::Send($PrinterName,$bytes)
$result | ConvertTo-Json -Compress
