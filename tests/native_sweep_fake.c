/* Actual native loop with deterministic fake USB. No device access. */
#define WinUsb_WritePipe FakeWrite
#define WinUsb_ReadPipe FakeRead
#define WinUsb_GetOverlappedResult FakeResult
#define WinUsb_SetPipePolicy FakePolicy
#include "../scripts/position_sweep/native_engine.c"

static unsigned char queued[76];
static int available, mode, sends, pd_seen;
static PUCHAR waiting;
static LPOVERLAPPED waiting_ov;
static LPOVERLAPPED waiting_tx;
static float position;
static void deliver(void) {
    if (available && waiting) {
        memcpy(waiting,queued,76); available=0; waiting=NULL;
        waiting_ov->InternalHigh=76; SetEvent(waiting_ov->hEvent);
    }
}
BOOL WINAPI FakePolicy(WINUSB_INTERFACE_HANDLE u,UCHAR p,ULONG k,ULONG n,PVOID v) {
    (void)u;(void)p;(void)k;(void)n;(void)v;return TRUE;
}
BOOL WINAPI FakeWrite(WINUSB_INTERFACE_HANDLE u,UCHAR pipe,PUCHAR data,ULONG size,PULONG n,LPOVERLAPPED ov) {
    (void)u;(void)pipe;
    if(n) *n=size;
    if(ov) {
        ov->InternalHigh=mode==7 ? size-1 : size;
        if(mode==6) waiting_tx=ov;
        else SetEvent(ov->hEvent);
    }
    memset(queued,0,76); uint32_t echo=0xffffffff,id;
    memcpy(queued,&echo,4); queued[10]=6;
    if (u32(data+4)>=0x600) {
        id=0x581; queued[8]=8;
        memcpy(queued+12,"\x60\x02\x20\x00",4);
    } else {
        sends++;
        if (mode==1 && sends>5) return TRUE;
        id=0x191; queued[8]=11;
        position=mode==4 ? 1.f : f32(data+12);
        if(u16(data+24)) pd_seen=1;
        int pure_ff=u16(data+24)==0 && u16(data+26)==0;
        if(mode==10 && pure_ff) position=1;
        memcpy(queued+12,&position,4);
        memcpy(queued+20,data+20,4);
        if(mode==9) { float velocity=10; memcpy(queued+16,&velocity,4); }
        if(mode==11 && pure_ff) { float velocity=10; memcpy(queued+16,&velocity,4); }
        if(mode==8 && pure_ff && (!pd_seen || f32(data+12)!=0 || f32(data+16)!=0)) {
            SetLastError(ERROR_INVALID_DATA); return FALSE;
        }
        if(mode==8 && !pure_ff && f32(data+20)!=0) { SetLastError(ERROR_INVALID_DATA); return FALSE; }
        int16_t temp=(mode==3 && sends>5)?850:260;
        memcpy(queued+24,&temp,2); memcpy(queued+26,&temp,2);
        if(mode==2 && sends>5) queued[28]=1;
    }
    memcpy(queued+4,&id,4); available=1; deliver();
    if(mode==6 && ov) { SetLastError(ERROR_IO_PENDING); return FALSE; }
    return TRUE;
}
BOOL WINAPI FakeRead(WINUSB_INTERFACE_HANDLE u,UCHAR pipe,PUCHAR data,ULONG size,PULONG n,LPOVERLAPPED ov) {
    (void)u;(void)pipe;(void)size;(void)n;
    waiting=data; waiting_ov=ov; deliver();
    if(waiting) { SetLastError(ERROR_IO_PENDING); return FALSE; }
    return TRUE;
}
BOOL WINAPI FakeResult(WINUSB_INTERFACE_HANDLE u,LPOVERLAPPED ov,LPDWORD n,BOOL wait) {
    (void)u;(void)wait; *n=(DWORD)ov->InternalHigh;
    if(waiting_tx) { SetEvent(waiting_tx->hEvent); waiting_tx=NULL; }
    return TRUE;
}
API int simulate(int scenario,const Config *c,Row *rows,uint64_t capacity,Stats *s,Control *ctl) {
    available=sends=pd_seen=0; waiting=NULL; waiting_tx=NULL; mode=scenario; position=0;
    return sweep_run(NULL,NULL,0x81,2,c,rows,capacity,s,ctl);
}
