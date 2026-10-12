/* J60 classic-CAN single-owner WinUSB sweep. No Python timing or disk I/O. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winusb.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#define API __declspec(dllexport)
typedef struct {
    double rate, f0, f1, duration, amplitude, ramp, home_position, home_duration;
    double settle, home_timeout, position_tolerance, velocity_tolerance;
    double motor_temperature, driver_temperature, feedback_timeout, lease;
    uint32_t id, kp, kd, derivative;
    double max_travel, max_velocity;
    uint32_t feedforward;
} Config;
typedef struct { double target, kp, kd, position, velocity, torque, mt, bt, at, frequency; } Row;
typedef struct {
    uint64_t tx, rx, rows, skipped, gaps, limited, counters;
    double first_tx, last_tx, max_gap, max_write, first_rx, last_rx, max_submit, motion_origin;
    uint32_t code, win_error, disable_ack, can_error_id, status_word;
    unsigned char can_error_data[8];
} Stats;
typedef struct { volatile LONG stop; volatile LONG64 heartbeat, frequency_bits; } Control;
static double seconds(void) {
    LARGE_INTEGER n, f; QueryPerformanceCounter(&n); QueryPerformanceFrequency(&f);
    return (double)n.QuadPart / f.QuadPart;
}
API void sweep_heartbeat(Control *c) {
    LARGE_INTEGER n; QueryPerformanceCounter(&n); InterlockedExchange64(&c->heartbeat, n.QuadPart);
}
API void sweep_stop(Control *c) { InterlockedExchange(&c->stop, 1); }
static void publish_frequency(Control *c, double frequency) {
    LONG64 bits; memcpy(&bits,&frequency,sizeof(bits));
    InterlockedExchange64(&c->frequency_bits,bits);
}
API double sweep_current_frequency(Control *c) {
    LONG64 bits=InterlockedCompareExchange64(&c->frequency_bits,0,0);
    double frequency; memcpy(&frequency,&bits,sizeof(frequency)); return frequency;
}
API double sweep_frequency(const Config *c, double t) {
    if (t>=c->duration+2*c->ramp) return 0;
    if (t<c->ramp) return c->f0;
    if (t<c->ramp+c->duration) return c->f0+(c->f1-c->f0)*(t-c->ramp)/c->duration;
    return c->f1;
}
API uint32_t sweep_size(uint32_t i) {
    return i == 0 ? sizeof(Config) : i == 1 ? sizeof(Row) : i == 2 ? sizeof(Stats) : sizeof(Control);
}
static double envelope(double u) {
    u = fmax(0, fmin(1, u)); return u*u*u*(10+u*(-15+6*u));
}
static double derivative(double u) {
    u = fmax(0, fmin(1, u)); return 30*u*u*(1-u)*(1-u);
}
API void sweep_sample(const Config *c, double t, double *p, double *v) {
    const double tau = 6.2831853071795864769;
    double g, dg, frequency, cycles, x, slope = (c->f1-c->f0)/c->duration;
    t = fmax(0, t);
    if (t >= c->duration+2*c->ramp) { *p = *v = 0; return; }
    if (t < c->ramp) {
        g=envelope(t/c->ramp); dg=derivative(t/c->ramp)/c->ramp;
        frequency=c->f0; cycles=c->f0*t;
    } else if (t < c->ramp+c->duration) {
        x=t-c->ramp; g=1; dg=0; frequency=c->f0+slope*x;
        cycles=c->f0*c->ramp+c->f0*x+.5*slope*x*x;
    } else {
        x=t-c->ramp-c->duration; g=1-envelope(x/c->ramp);
        dg=-derivative(x/c->ramp)/c->ramp; frequency=c->f1;
        cycles=c->f0*c->ramp+.5*(c->f0+c->f1)*c->duration+c->f1*x;
    }
    double angle=tau*remainder(cycles,1);
    *p=c->amplitude*g*sin(angle);
    *v=c->derivative ? c->amplitude*(dg*sin(angle)+g*tau*frequency*cos(angle)) : 0;
}

static uint32_t u32(const unsigned char *p) { uint32_t n; memcpy(&n,p,4); return n; }
static uint64_t u64(const unsigned char *p) { uint64_t n; memcpy(&n,p,8); return n; }
static double decoded(uint64_t q, double lo, double hi, unsigned bits) {
    return (double)q*(hi-lo)/(double)((1ULL<<bits)-1)+lo;
}
static uint64_t encoded(double value, double lo, double hi, unsigned bits) {
    return (uint64_t)((fmax(lo,fmin(hi,value))-lo)*((1ULL<<bits)-1)/(hi-lo));
}
API void j60_pack(double p, double v, double torque, double kp, double kd, unsigned char *out) {
    uint64_t n=encoded(p,-40,40,16) | (encoded(v,-40,40,14)<<16)
        | (encoded(kp,0,1023,10)<<30) | (encoded(kd,0,51,8)<<40)
        | (encoded(torque,-40,40,16)<<48);
    memcpy(out,&n,8);  /* Windows x64 little endian; no compiler bitfields. */
}
static void decode_motion(const unsigned char *p, double *pos, double *vel, double *torque,
                          double *mt, double *bt) {
    uint64_t n=u64(p);
    *pos=decoded(n&0xfffff,-40,40,20);
    *vel=decoded((n>>20)&0xfffff,-40,40,20);
    *torque=decoded((n>>40)&0xffff,-40,40,16);
    double temperature=decoded((n>>57)&0x7f,-20,200,7);
    if ((n>>56)&1) *mt=temperature; else *bt=temperature;
}
static int write_packet(void *usb, UCHAR pipe, unsigned char *p, Stats *s) {
    ULONG n=0; double at=seconds();
    BOOL ok=WinUsb_WritePipe(usb,pipe,p,20,&n,NULL);
    s->max_write=fmax(s->max_write,seconds()-at);
    if (!ok || n!=20) { s->win_error=ok ? ERROR_WRITE_FAULT : GetLastError(); return 0; }
    return 1;
}
static int finish_tx(void *usb, OVERLAPPED *ov, Stats *s, double started, int kind,
                     double next_target, double *target, double *due, double period) {
    ULONG n=0;
    BOOL ok=WinUsb_GetOverlappedResult(usb,ov,&n,FALSE);
    if (!ok || n!=20) {
        s->win_error=ok ? ERROR_WRITE_FAULT : GetLastError(); return 0;
    }
    double at=seconds(); s->max_write=fmax(s->max_write,at-started);
    if (kind!=4) return 1;  /* Status queries do not consume a motion deadline. */
    *target=next_target;
    if (s->tx) { double gap=at-s->last_tx; s->max_gap=fmax(s->max_gap,gap); if(gap>.005)s->gaps++; }
    else s->first_tx=at;
    s->last_tx=at; s->tx++;
    double steps=fmax(1,floor((at-*due)/period)+1);
    s->skipped+=(uint64_t)steps-1; *due+=steps*period;
    return 1;
}
static int save_row(Row *rows, uint64_t capacity, Stats *s, double target,
                    double pos, double vel, double torque, double mt, double bt,
                    double at, double frequency) {
    if (s->rows>=capacity) { if(!s->code)s->code=9; return 0; }
    rows[s->rows++]=(Row){target,0,0,pos,vel,torque,mt,bt,at,frequency};
    if(s->rows==1)s->first_rx=at;
    s->last_rx=at;
    return 1;
}

/* Common position_sweep error codes plus 12: periodic status reply timeout. */
API int sweep_run(void *usb, void *file, unsigned input, unsigned output,
                  const Config *c, Row *rows, uint64_t capacity, Stats *s, Control *ctl) {
    unsigned char incoming[2048], buffer[4096], tx[20]={0}, disable[20]={0};
    size_t buffered=0;
    OVERLAPPED ov={0}, txov={0};
    int pending=0, tx_pending=0, tx_kind=4, stage=0, capture=0, done=0, inflight=0;
    int next_feedforward=0, status_waiting=0;
    double tx_started=0, next_target=0, target=0, next_frequency=0, target_frequency=0;
    double mt=NAN,bt=NAN,pos=0,vel=0,torque=0;
    uint32_t echo=1;
    double start=seconds(), due=start, last_rx=start, stable=-1;
    double period=1/c->rate, end=start+c->home_duration, status_due=start+1,status_sent=0;
    LARGE_INTEGER qpc_frequency; QueryPerformanceFrequency(&qpc_frequency);
    memset(s,0,sizeof(*s));
    ov.hEvent=CreateEventW(NULL,TRUE,FALSE,NULL);
    txov.hEvent=CreateEventW(NULL,TRUE,FALSE,NULL);
    if(!ov.hEvent || !txov.hEvent){s->code=2;s->win_error=GetLastError();goto stop_motor;}
    SetThreadPriority(GetCurrentThread(),THREAD_PRIORITY_ABOVE_NORMAL);
    { ULONG timeout=50;
      if(!WinUsb_SetPipePolicy(usb,(UCHAR)output,PIPE_TRANSFER_TIMEOUT,sizeof(timeout),&timeout)){
          s->code=2;s->win_error=GetLastError();goto stop_motor;
      }
    }
    while(!done){
        double now=seconds();
        if(InterlockedCompareExchange(&ctl->stop,0,0)){s->code=1;break;}
        double heartbeat=(double)InterlockedCompareExchange64(&ctl->heartbeat,0,0)/qpc_frequency.QuadPart;
        if(now-heartbeat>c->lease){s->code=7;break;}
        if(now-last_rx>c->feedback_timeout){s->code=6;break;}
        if(status_waiting && now-status_sent>.2){s->code=12;break;}
        if(!stage && now>end+c->home_timeout){s->code=8;break;}
        if(stage && now>=end)break;
        if(tx_pending){
            DWORD ready=WaitForSingleObject(txov.hEvent,0);
            if(ready==WAIT_OBJECT_0){
                tx_pending=0;
                if(!finish_tx(usb,&txov,s,tx_started,tx_kind,next_target,&target,&due,period)){s->code=2;break;}
                if(tx_kind==4){
                    target_frequency=next_frequency;publish_frequency(ctl,target_frequency);
                    if(next_feedforward)capture=1;
                }
            }else if(ready!=WAIT_TIMEOUT || now-tx_started>.05){
                s->code=2;s->win_error=ready==WAIT_TIMEOUT?ERROR_SEM_TIMEOUT:GetLastError();break;
            }
        }
        if(!tx_pending && ((!status_waiting && now>=status_due) || now>=due)){
            tx_kind=(!status_waiting && now>=status_due)?23:4;
            if(tx_kind==4 && inflight>=4){s->limited++;due=now+period;}
            else{
                memset(tx,0,sizeof(tx));
                memcpy(tx,&echo,4);echo=echo%255+1;
                uint32_t id=((uint32_t)tx_kind<<5)|c->id;memcpy(tx+4,&id,4);
                if(tx_kind==23){status_waiting=1;status_sent=now;status_due=now+1;}
                else{
                    double p,v,t=now-start;
                    next_frequency=stage?sweep_frequency(c,t):0;
                    if(stage)sweep_sample(c,t,&p,&v);
                    else{p=c->home_position*(1-envelope(t/c->home_duration));v=0;}
                    next_feedforward=stage;
                    next_target=(float)p;
                    tx[8]=8;  /* Classic standard CAN, gs_usb flags remain zero. */
                    if(stage)j60_pack(0,0,next_target,0,0,tx+12);
                    else j60_pack(next_target,v,0,c->kp/100.,c->kd/100.,tx+12);
                    inflight++;
                }
                ResetEvent(txov.hEvent);tx_started=seconds();
                BOOL ok=WinUsb_WritePipe(usb,(UCHAR)output,tx,20,NULL,&txov);
                DWORD error=ok?0:GetLastError();
                s->max_submit=fmax(s->max_submit,seconds()-tx_started);
                if(!ok && error!=ERROR_IO_PENDING){s->code=2;s->win_error=error;break;}
                tx_pending=1;
                if(ok || WaitForSingleObject(txov.hEvent,0)==WAIT_OBJECT_0){
                    tx_pending=0;
                    if(!finish_tx(usb,&txov,s,tx_started,tx_kind,next_target,&target,&due,period)){s->code=2;break;}
                    if(tx_kind==4){target_frequency=next_frequency;publish_frequency(ctl,target_frequency);if(next_feedforward)capture=1;}
                }
            }
        }
        if(!pending){
            ResetEvent(ov.hEvent);
            BOOL ok=WinUsb_ReadPipe(usb,(UCHAR)input,incoming,sizeof(incoming),NULL,&ov);
            if(!ok && GetLastError()!=ERROR_IO_PENDING){s->code=2;s->win_error=GetLastError();break;}
            pending=1;
        }
        DWORD ready=WaitForSingleObject(ov.hEvent,0);
        if(ready==WAIT_OBJECT_0){
            ULONG n=0;
            if(!WinUsb_GetOverlappedResult(usb,&ov,&n,FALSE)){s->code=2;s->win_error=GetLastError();break;}
            pending=0;
            if(buffered+n>sizeof(buffer)){s->code=3;break;}
            memcpy(buffer+buffered,incoming,n);buffered+=n;
            while(buffered>=12){
                unsigned char packet[76];size_t len=(buffer[10]&2)?76:20;
                if(buffered<len)break;
                memcpy(packet,buffer,len);buffered-=len;memmove(buffer,buffer+len,buffered);
                unsigned char *p=packet;uint32_t id=u32(p+4);
                if(p[9]!=0 || (p[10]&1) || p[8]>((p[10]&2)?15:8)){s->code=3;done=1;break;}
                if(u32(p)!=0xffffffff)continue; /* USB TX echo is not a motor response. */
                if(id&0x20000000){
                    if(id==0x20000000 && !(p[10]&2) && p[8]==8 && !memcmp(p+12,"\0\0\0\0\0\0",6) && p[18]<96 && p[19]<96)s->counters++;
                    else{s->can_error_id=id&0x1fffffff;memcpy(s->can_error_data,p+12,8);s->code=3;done=1;break;}
                }else if(id==(0x2f0|c->id)){
                    if(p[10] || (p[8]!=2 && p[8]!=5 && p[8]!=8)){s->code=3;done=1;break;}
                    status_waiting=0;s->status_word=p[12]|((uint32_t)p[13]<<8);
                    if(s->status_word){s->code=4;done=1;break;}
                }else if(id==(0x90|c->id)){
                    if(p[10] || p[8]!=8){s->code=3;done=1;break;}
                    double at=seconds();decode_motion(p+12,&pos,&vel,&torque,&mt,&bt);
                    s->rx++;last_rx=at;if(inflight)inflight--;
                    if(capture && !save_row(rows,capacity,s,target,pos,vel,torque,mt,bt,at,target_frequency)){done=1;break;}
                    if(mt>=c->motor_temperature || bt>=c->driver_temperature){s->code=5;done=1;break;}
                    if(stage && ((c->max_travel>0 && fabs(pos-s->motion_origin)>=c->max_travel) || fabs(vel)>=c->max_velocity)){s->code=11;done=1;break;}
                    if(!stage && at>=end){
                        if(fabs(pos)<=c->position_tolerance && fabs(vel)<=c->velocity_tolerance){
                            if(stable<0)stable=at;
                            if(at-stable>=c->settle){stage=1;s->motion_origin=pos;start=at;end=at+c->duration+2*c->ramp;stable=-1;}
                        }else stable=-1;
                    }
                }
            }
        }else if(ready!=WAIT_TIMEOUT){s->code=2;s->win_error=GetLastError();break;}
        else if(due-seconds()>.002)Sleep(1);
        else YieldProcessor();
    }
stop_motor:
    if(tx_pending){
        ULONG n=0;CancelIoEx(file,&txov);
        if(WinUsb_GetOverlappedResult(usb,&txov,&n,TRUE) && n==20 && tx_kind==4){target=next_target;target_frequency=next_frequency;}
    }
    {uint32_t id=0x20|c->id;memcpy(disable,&echo,4);memcpy(disable+4,&id,4);}
    if(write_packet(usb,(UCHAR)output,disable,s) && ov.hEvent){
        double deadline=seconds()+.2;
        while(seconds()<deadline && !s->disable_ack){
            while(buffered>=12){
                unsigned char *p=buffer;size_t len=(p[10]&2)?76:20;if(buffered<len)break;
                if(u32(p)==0xffffffff && p[9]==0 && p[10]==0){
                    uint32_t id=u32(p+4);
                    if(id==(0x30|c->id) && p[8]==1 && p[12]==0)s->disable_ack=1;
                    else if(capture && id==(0x90|c->id) && p[8]==8){
                        double at=seconds();decode_motion(p+12,&pos,&vel,&torque,&mt,&bt);
                        save_row(rows,capacity,s,target,pos,vel,torque,mt,bt,at,target_frequency);s->rx++;
                    }
                }
                buffered-=len;memmove(buffer,buffer+len,buffered);
            }
            if(s->disable_ack)break;
            if(!pending){
                ResetEvent(ov.hEvent);
                BOOL ok=WinUsb_ReadPipe(usb,(UCHAR)input,incoming,sizeof(incoming),NULL,&ov);
                if(!ok && GetLastError()!=ERROR_IO_PENDING)break;
                pending=1;
            }
            if(WaitForSingleObject(ov.hEvent,1)==WAIT_OBJECT_0){
                ULONG n=0;if(!WinUsb_GetOverlappedResult(usb,&ov,&n,FALSE))break;pending=0;
                if(buffered+n>sizeof(buffer))break;
                memcpy(buffer+buffered,incoming,n);buffered+=n;
            }
        }
    }
    if(!s->disable_ack && !s->code)s->code=10;
    if(pending){ULONG n=0;CancelIoEx(file,&ov);WinUsb_GetOverlappedResult(usb,&ov,&n,TRUE);}
    if(ov.hEvent)CloseHandle(ov.hEvent);
    if(txov.hEvent)CloseHandle(txov.hEvent);
    return (int)s->code;
}
