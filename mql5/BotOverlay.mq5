//+------------------------------------------------------------------+
//|                                                 BotOverlay.mq5   |
//|                                       GoldManager — XAUUSD Bot   |
//+------------------------------------------------------------------+
// Reads overlay_levels.json from MQL5/Files/ every POLL_INT sec and
// draws: 3 VWAPs, 6 Volume Profiles (VAH/VPOC/VAL + value-area rect),
// N FVG rectangles. Robust to missing/corrupt JSON, null fields,
// missing keys, and prev_*=null (AGENTS.md §4b-1).
//+------------------------------------------------------------------+
#property copyright "GoldManager"
#property version   "1.00"
#property description "XAUUSD Bot — feature overlay (VWAP + VP + FVG)"
#property strict

const string PREFIX       = "bot_";
const string OVERLAY_FILE = "overlay_levels.json";
const int    POLL_INT     = 5;
const string OBJ_VWAP[]   = {"vwap_utc00","vwap_utc07","vwap_utc12"};
const string VP_PERIODS[] = {"weekly","monthly","yearly",
                             "prev_week","prev_month","prev_year"};
const color C_VW0=clrDodgerBlue, C_VW7=clrOrange, C_VW12=clrMagenta;
const color C_VD=clrGray, C_VL=clrWhite, C_VP=clrYellow;
const color C_BULL=clrGreen, C_BEAR=clrRed;

//--- Event handlers ---------------------------------------------------
int OnInit(){ EventSetTimer(POLL_INT); DrawOverlay(); return INIT_SUCCEEDED; }
void OnDeinit(const int r){ EventKillTimer(); ClearAll(); }
void OnTimer(){ DrawOverlay(); }
void OnChartEvent(const int id,const long &lp,const double &dp,const string &sp){
   if(id==CHARTEVENT_CHART_CHANGE) DrawOverlay(); }

//--- Cleanup ----------------------------------------------------------
void ClearAll(){
   for(int i=ObjectsTotal(0)-1;i>=0;i--)
      if(StringFind(ObjectName(0,i),PREFIX)==0) ObjectDelete(0,ObjectName(0,i)); }

//--- File I/O ---------------------------------------------------------
bool ReadFile(string &out){
   int h=FileOpen(OVERLAY_FILE,FILE_READ|FILE_TXT|FILE_ANSI,0,CP_UTF8);
   if(h==INVALID_HANDLE){ Print("BotOverlay: ",OVERLAY_FILE," missing"); return false; }
   out="";
   while(!FileIsEnding(h)) out+=FileReadString(h);
   FileClose(h);
   return StringLen(out)>0; }

//--- JSON helpers (MQL5 stdlib has no JsonParse — substring is enough
//    since the writer is Python json.dump with a stable schema).
bool JNum(const string &j,const string &k,double &v){
   int p=StringFind(j,"\""+k+"\""); if(p<0) return false;
   p+=StringLen(k)+2;
   while(p<StringLen(j)){ ushort c=StringGetCharacter(j,p);
      if(c!=':'&&c!=' '&&c!='\t'&&c!='\n'&&c!='\r') break; p++; }
   if(p>=StringLen(j)) return false;
   ushort f=StringGetCharacter(j,p);
   if(f=='n'||f==','||f=='}') return false;       // null
   string num="";
   while(p<StringLen(j)){ ushort c=StringGetCharacter(j,p);
      if(c==','||c=='}'||c==' '||c=='\n'||c=='\r') break;
      num+=ShortToString(c); p++; }
   if(StringLen(num)==0) return false;
   v=StringToDouble(num); return true; }

bool JStr(const string &j,const string &k,string &v){
   int p=StringFind(j,"\""+k+"\""); if(p<0) return false;
   p+=StringLen(k)+2;
   while(p<StringLen(j)){ ushort c=StringGetCharacter(j,p);
      if(c!=':'&&c!=' '&&c!='\t'&&c!='\n'&&c!='\r') break; p++; }
   if(p>=StringLen(j)||StringGetCharacter(j,p)!='"') return false;
   p++; v="";
   while(p<StringLen(j)&&StringGetCharacter(j,p)!='"')
      { v+=ShortToString(StringGetCharacter(j,p)); p++; }
   return StringLen(v)>0; }

bool JObj(const string &j,const string &k,string &b){
   int p=StringFind(j,"\""+k+"\""); if(p<0) return false;
   p+=StringLen(k)+2;
   while(p<StringLen(j)){
      if(StringGetCharacter(j,p)=='{') break;
      ushort c=StringGetCharacter(j,p);
      if(c=='n'||c==',') return false;            // null
      p++; }
   if(p>=StringLen(j)) return false;
   int depth=0, start=p;
   while(p<StringLen(j)){ ushort c=StringGetCharacter(j,p);
      if(c=='{') depth++;
      else if(c=='}'){ depth--; if(depth==0)
         { b=StringSubstr(j,start,p-start+1); return true; } }
      p++; }
   return false; }

//--- Drawing primitives ----------------------------------------------
void HLine(const string n,double p,color c,int s){
   string f=PREFIX+n;
   if(ObjectFind(0,f)>=0) ObjectDelete(0,f);
   if(p!=p) return;                               // NaN skip; negative OK (backtest)
   ObjectCreate(0,f,OBJ_HLINE,0,0,p);
   ObjectSetInteger(0,f,OBJPROP_COLOR,c);
   ObjectSetInteger(0,f,OBJPROP_STYLE,s);
   ObjectSetInteger(0,f,OBJPROP_WIDTH,1);
   ObjectSetInteger(0,f,OBJPROP_BACK,false);
   ObjectSetInteger(0,f,OBJPROP_SELECTABLE,false); }

void Rect(const string n,double top,double bot,color c,bool back){
   string f=PREFIX+n;
   if(ObjectFind(0,f)>=0) ObjectDelete(0,f);
   if(top!=top||bot!=bot||top<=bot) return;
   datetime t1=TimeCurrent()-86400*30, t2=TimeCurrent()+300;
   ObjectCreate(0,f,OBJ_RECTANGLE,0,t1,top,t2,bot);
   ObjectSetInteger(0,f,OBJPROP_COLOR,c);
   ObjectSetInteger(0,f,OBJPROP_FILL,true);
   ObjectSetInteger(0,f,OBJPROP_BACK,back);
   ObjectSetInteger(0,f,OBJPROP_SELECTABLE,false); }

void Lbl(const string n,double p,const string t){
   string f=PREFIX+n;
   if(ObjectFind(0,f)>=0) ObjectDelete(0,f);
   ObjectCreate(0,f,OBJ_LABEL,0,0,0);
   ObjectSetInteger(0,f,OBJPROP_CORNER,CORNER_LEFT_UPPER);
   ObjectSetDouble(0,f,OBJPROP_PRICE,p);
   ObjectSetString(0,f,OBJPROP_TEXT,t);
   ObjectSetString(0,f,OBJPROP_FONT,"Arial");
   ObjectSetInteger(0,f,OBJPROP_FONTSIZE,8);
   ObjectSetInteger(0,f,OBJPROP_COLOR,clrWhite);
   ObjectSetInteger(0,f,OBJPROP_SELECTABLE,false); }

//--- Main draw -------------------------------------------------------
void DrawOverlay(){
   ClearAll();
   string j="";
   if(!ReadFile(j)) return;

   // 1. VWAPs (try nested "vwap":{...} first, fall back to top-level).
   string vw="";
   bool nested=JObj(j,"vwap",vw);
   const string scope=nested?vw:j;
   const string vk[]={"utc00","utc07","utc12"};
   const color  vc[]={C_VW0,C_VW7,C_VW12};
   for(int i=0;i<3;i++){ double v=0;
      if(JNum(scope,vk[i],v)) HLine(OBJ_VWAP[i],v,vc[i],STYLE_SOLID); }

   // 2. Volume profiles (weekly/monthly/yearly + prev_*).
   string vp="";
   if(!JObj(j,"volume_profile",vp)) return;
   for(int i=0;i<6;i++){
      string per=VP_PERIODS[i], pb="";
      if(!JObj(vp,per,pb)) continue;              // null prev_* => skip
      double vah=0,vpoc=0,val=0;
      bool hasV=JNum(pb,"vah",vah), hasP=JNum(pb,"vpoc",vpoc), hasL=JNum(pb,"val",val);
      string st=""; JStr(pb,"state",st);
      bool prev=(StringFind(per,"prev_")==0);
      bool dev =(st=="developing");
      color clr=prev?C_VP:(dev?C_VD:C_VL);
      int    stl=dev?STYLE_DOT:STYLE_SOLID;
      if(hasV){ HLine("vp_"+per+"_vah",vah,clr,stl);
                Lbl("vp_"+per+"_vah_l",vah,per+" VAH "+DoubleToString(vah,1)); }
      if(hasP){ HLine("vp_"+per+"_vpoc",vpoc,clr,stl);
                Lbl("vp_"+per+"_vpoc_l",vpoc,per+" VPOC "+DoubleToString(vpoc,1)); }
      if(hasL){ HLine("vp_"+per+"_val",val,clr,stl);
                Lbl("vp_"+per+"_val_l",val,per+" VAL "+DoubleToString(val,1)); }
      if(hasV&&hasL&&vah>val) Rect("va_"+per,vah,val,clr,dev); }

   // 3. FVG zones — each { ... } in fvg_zones array.
   string arr="";
   if(!JObj(j,"fvg_zones",arr)) return;
   int pos=0, idx=0;
   while(true){
      int op=StringFind(arr,"{",pos); if(op<0) break;
      int depth=0, end=op;
      for(int k=op;k<StringLen(arr);k++){ ushort c=StringGetCharacter(arr,k);
         if(c=='{') depth++;
         else if(c=='}'){ depth--; if(depth==0){ end=k; break; } } }
      string z=StringSubstr(arr,op,end-op+1);
      double top=0,bot=0; string zt="";
      JStr(z,"type",zt);
      if(JNum(z,"top",top)&&JNum(z,"bottom",bot)){
         color c=(zt=="bearish")?C_BEAR:C_BULL;
         Rect("fvg_"+IntegerToString(idx),top,bot,c,true); }
      idx++; pos=end+1; } }
//+------------------------------------------------------------------+