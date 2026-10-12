/* Execute the actual J60 native loop against deterministic fake USB. */
#define WinUsb_WritePipe FakeWrite
#define WinUsb_ReadPipe FakeRead
#define WinUsb_GetOverlappedResult FakeResult
#define WinUsb_SetPipePolicy FakePolicy
#include "../native_engine.c"

static unsigned char queued[40];
static int available, mode, sends, pd_seen, queries, sends_at_query;
static int zero_seen, sweep_seen;
static PUCHAR waiting;
static LPOVERLAPPED waiting_ov, waiting_tx;
static void deliver(void){
    if(available && waiting){
        memcpy(waiting,queued,(size_t)available);waiting_ov->InternalHigh=(ULONG)available;
        available=0;waiting=NULL;SetEvent(waiting_ov->hEvent);
    }
}
BOOL WINAPI FakePolicy(WINUSB_INTERFACE_HANDLE u,UCHAR p,ULONG k,ULONG n,PVOID v){
    (void)u;(void)p;(void)k;(void)n;(void)v;return TRUE;
}
BOOL WINAPI FakeWrite(WINUSB_INTERFACE_HANDLE u,UCHAR pipe,PUCHAR data,ULONG size,PULONG n,LPOVERLAPPED ov){
    (void)u;(void)pipe;
    if(size!=20 || data[10]!=0 || data[9]!=0){SetLastError(ERROR_INVALID_DATA);return FALSE;}
    unsigned command=u32(data+4)>>5;
    if(n)*n=size;
    if(ov){ov->InternalHigh=mode==7?size-1:size;if(mode==6)waiting_tx=ov;else SetEvent(ov->hEvent);}
    memset(queued,0,sizeof(queued));uint32_t echo=0xffffffff,id;
    memcpy(queued,&echo,4);
    if(command==1){
        if(data[8]!=0){SetLastError(ERROR_INVALID_DATA);return FALSE;}
        id=0x31;queued[8]=1;queued[12]=mode==12?1:0;
    }else if(command==23){
        if(data[8]!=0){SetLastError(ERROR_INVALID_DATA);return FALSE;}
        queries++;sends_at_query=sends;
        if(mode==21)return TRUE;
        id=0x2f1;queued[8]=2;queued[12]=mode==20?1:0;
    }else if(command==4){
        if(data[8]!=8){SetLastError(ERROR_INVALID_DATA);return FALSE;}
        sends++;if(mode==1 && sends>5)return TRUE;
        uint64_t raw=u64(data+12),qp=raw&0xffff,qv=(raw>>16)&0x3fff;
        uint64_t kp=(raw>>30)&0x3ff,kd=(raw>>40)&0xff,qt=(raw>>48)&0xffff;
        int pure=kp==0 && kd==0;
        if(kp)pd_seen=1;
        if(mode==8 && ((pure && (!pd_seen || qp!=32767 || qv!=8191)) || (!pure && qt!=32767))){
            SetLastError(ERROR_INVALID_DATA);return FALSE;
        }
        if(mode==23 && (pure || qt!=32767 || qv!=8191)){SetLastError(ERROR_INVALID_DATA);return FALSE;}
        if(abs((int)qp-32767)<=1)zero_seen=1;
        if(zero_seen && abs((int)qp-32767)>1)sweep_seen=1;
        double pos=pure?0:decoded(qp,-40,40,16),vel=0,torque=decoded(qt,-40,40,16);
        if(mode==4 || (mode==24 && sweep_seen && abs((int)qp-32767)<=1))pos=1;
        if(mode==10 && pure)pos=1;
        if(mode==11 && pure)vel=10;
        double temp=mode==3 && sends>5?100:26;
        uint64_t feedback=encoded(pos,-40,40,20)|(encoded(vel,-40,40,20)<<20)
            |(encoded(torque,-40,40,16)<<40)|((uint64_t)(sends&1)<<56)
            |(encoded(temp,-20,200,7)<<57);
        id=0x91;queued[8]=8;memcpy(queued+12,&feedback,8);
        if(mode==22){id=0x20000024;queued[12]=0;queued[13]=0x20;queued[18]=128;}
    }else{SetLastError(ERROR_INVALID_DATA);return FALSE;}
    memcpy(queued+4,&id,4);available=20;
    if(mode==13 && command==4){memcpy(queued+20,queued,20);available=40;}
    deliver();
    if(mode==6 && ov){SetLastError(ERROR_IO_PENDING);return FALSE;}
    return TRUE;
}
BOOL WINAPI FakeRead(WINUSB_INTERFACE_HANDLE u,UCHAR pipe,PUCHAR data,ULONG size,PULONG n,LPOVERLAPPED ov){
    (void)u;(void)pipe;(void)size;(void)n;waiting=data;waiting_ov=ov;deliver();
    if(waiting){SetLastError(ERROR_IO_PENDING);return FALSE;}return TRUE;
}
BOOL WINAPI FakeResult(WINUSB_INTERFACE_HANDLE u,LPOVERLAPPED ov,LPDWORD n,BOOL wait){
    (void)u;(void)wait;*n=(DWORD)ov->InternalHigh;
    if(waiting_tx){SetEvent(waiting_tx->hEvent);waiting_tx=NULL;}return TRUE;
}
API int simulate(int scenario,const Config *c,Row *rows,uint64_t capacity,Stats *s,Control *ctl){
    zero_seen=sweep_seen=0;available=sends=pd_seen=queries=sends_at_query=0;waiting=NULL;waiting_tx=NULL;mode=scenario;
    return sweep_run(NULL,NULL,0x81,2,c,rows,capacity,s,ctl);
}
API int queries_count(void){return queries;}
API int controls_after_query(void){return sends-sends_at_query;}
