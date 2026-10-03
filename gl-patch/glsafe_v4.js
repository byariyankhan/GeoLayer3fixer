/* GL-PATCH v4.1 2026-10-03: safe tile image writes + FULL PNG validation + canvas.toDataURL guard.
   v4.1: every PNG returned by canvas.toDataURL() in this panel is validated; a broken one is replaced by a
   PNG encoded from the same canvas pixels with Node zlib. Covers ALL GEOlayers tile paths (tile merger,
   GL renderer, zoom-level merge) - v4.0 only covered the zoom-level merge.
   v4: v3 only checked chunk CRCs. Chromium's canvas.toDataURL() in AE 25/26 sometimes returns PNGs whose
   CRCs are fine but whose compressed image data is broken (-> "bad adaptive filter value"). v4 inflates every
   PNG and checks every scanline before it is written; a broken encode is retried, and if it fails again the
   tile is encoded with Node's own zlib from the canvas pixels instead of Chromium's encoder.
   Fixes After Effects "AEGP Plugin PNGIO Support: IDAT: incorrect data check" /
   "bad adaptive filter value" / "IDAT: CRC error" (5027::12).
   Every tile is validated in memory, written to a temp file, read back and compared byte-for-byte,
   then renamed into place. Cached tiles are fully CRC-checked before re-use; broken ones are re-rendered.
   Log file: %APPDATA%\aescripts\GEOlayers3\gl-patch.log */
window.glSafeImageFile=(function(){
  var _fs=null,_path=null,_zlib=null,_crcT=null,_seq=0,_inflight={},_swept={},_logPath=null,_deepCache={};
  var _stats={toDataUrlBad:0,retryOk:0,ownEncoder:0};
  function fs(){return _fs||(_fs=require("fs"))}
  function zl(){return _zlib||(_zlib=require("zlib"))}
  function pth(){return _path||(_path=require("path"))}
  function logPath(){
    if(_logPath!==null)return _logPath;
    try{var base=process.env.APPDATA||(process.env.HOME?process.env.HOME+"/Library/Preferences":null);_logPath=base?pth().join(base,"aescripts","GEOlayers3","gl-patch.log"):""}catch(e){_logPath=""}
    return _logPath;
  }
  function log(m){
    try{window.glLogger&&glLogger.log&&glLogger.log("glSafeImageFile",m)}catch(e){}
    try{var p=logPath();if(!p)return;try{if(fs().statSync(p).size>2e6)fs().writeFileSync(p,"")}catch(e){}fs().appendFileSync(p,new Date().toISOString()+" "+m+"\r\n")}catch(e){}
  }
  function crc32(b,s,e){if(!_crcT){_crcT=[];for(var n=0;n<256;n++){var c=n;for(var k=0;k<8;k++)c=1&c?3988292384^c>>>1:c>>>1;_crcT[n]=c>>>0}}var c=-1;for(var i=s;i<e;i++)c=_crcT[255&(c^b[i])]^c>>>8;return(-1^c)>>>0}
  function isPng(b){return b.length>=8&&137===b[0]&&80===b[1]&&78===b[2]&&71===b[3]&&13===b[4]&&10===b[5]&&26===b[6]&&10===b[7]}
  function isJpg(b){return b.length>=4&&255===b[0]&&216===b[1]}
  function hex(b,s,e){return b.subarray(Math.max(0,s),Math.min(b.length,e)).toString("hex")}
  /* FULL check of an in-memory image: chunk CRCs and order, IHDR, then the IDAT stream is inflated and every
     scanline's filter byte and the exact image size are verified - the same things libpng in AE checks.
     Returns null when OK, else a description. */
  function checkBuffer(b){
    if(!b||!b.length)return"empty image data";
    if(isPng(b)){
      var off=8,ihdr=null,idats=[],idatRuns=0,prev="",iend=!1;
      while(off+12<=b.length){
        var len=b.readUInt32BE(off),type=b.toString("latin1",off+4,off+8);
        if(off+12+len>b.length)return"PNG truncated in chunk "+type+" at "+off+" (header "+hex(b,off,off+8)+")";
        if(b.readUInt32BE(off+8+len)!==crc32(b,off+4,off+8+len))return"PNG CRC mismatch in chunk "+type+" at "+off+" len "+len+" (header "+hex(b,off,off+8)+")";
        if("IHDR"===type)ihdr=b.subarray(off+8,off+8+len);
        else if("IDAT"===type){if("IDAT"!==prev)idatRuns++;idats.push(b.subarray(off+8,off+8+len))}
        prev=type;off+=12+len;
        if("IEND"===type){iend=!0;break}
      }
      if(!ihdr||ihdr.length<13||!idats.length||!iend)return"PNG incomplete (IHDR:"+!!ihdr+" IDAT:"+idats.length+" IEND:"+iend+")";
      if(off!==b.length)return"PNG has trailing data";
      if(idatRuns>1)return"PNG IDAT chunks not consecutive";
      var w=ihdr.readUInt32BE(0),h=ihdr.readUInt32BE(4),depth=ihdr[8],ctype=ihdr[9],interlace=ihdr[12];
      var ch={0:1,2:3,3:1,4:2,6:4}[ctype];
      if(!w||!h||!ch)return"PNG bad IHDR ("+w+"x"+h+" type "+ctype+")";
      var z=Buffer.concat(idats),res,raw;
      try{res=zl().inflateSync(z,{info:!0})}catch(e){return"PNG image data corrupt (zlib: "+(e&&e.message)+")"}
      raw=res&&res.buffer?res.buffer:res;
      if(res&&res.engine&&"number"==typeof res.engine.bytesWritten&&res.engine.bytesWritten<z.length)return"PNG extra data after image stream";
      if(0===interlace){
        var rl=Math.ceil(w*ch*depth/8)+1,expected=rl*h;
        if(raw.length!==expected)return"PNG image data size "+raw.length+" != expected "+expected;
        for(var r=0;r<h;r++)if(raw[r*rl]>4)return"PNG bad adaptive filter value "+raw[r*rl]+" in row "+r;
      }
      return null;
    }
    if(isJpg(b))return 255===b[b.length-2]&&217===b[b.length-1]?null:"JPEG missing EOI marker";
    return"unknown image format";
  }
  function chunk(type,data){
    var hdr=Buffer.alloc(8),out;hdr.writeUInt32BE(data.length,0);hdr.write(type,4,"latin1");
    out=Buffer.concat([hdr,data,Buffer.alloc(4)]);
    out.writeUInt32BE(crc32(out,4,8+data.length),8+data.length);return out;
  }
  /* Our own PNG encoder (RGBA 8-bit, "Sub" filter, Node zlib) - used when Chromium's encoder fails. */
  function encodeRgbaPng(w,h,px){
    var stride=4*w,rl=stride+1,raw=Buffer.allocUnsafe(rl*h);
    for(var y=0;y<h;y++){
      var o=y*rl,s=y*stride,x;raw[o]=1;
      for(x=0;x<4;x++)raw[o+1+x]=px[s+x];
      for(x=4;x<stride;x++)raw[o+1+x]=px[s+x]-px[s+x-4]&255;
    }
    var z=zl().deflateSync(raw,{level:3});
    var ihdr=Buffer.alloc(13),parts=[Buffer.from([137,80,78,71,13,10,26,10])];
    ihdr.writeUInt32BE(w,0);ihdr.writeUInt32BE(h,4);ihdr[8]=8;ihdr[9]=6;ihdr[10]=0;ihdr[11]=0;ihdr[12]=0;
    parts.push(chunk("IHDR",ihdr),chunk("sRGB",Buffer.from([0])));
    for(var i=0;i<z.length;i+=1048576)parts.push(chunk("IDAT",z.subarray(i,Math.min(z.length,i+1048576))));
    parts.push(chunk("IEND",Buffer.alloc(0)));
    return Buffer.concat(parts);
  }
  /* RGBA pixels of a 2D or WebGL canvas */
  function pixelsOf(canvas){
    var w=canvas.width,h=canvas.height,ctx=null;
    try{ctx=canvas.getContext("2d")}catch(e){}
    if(!ctx){
      var c2=document.createElement("canvas");c2.width=w;c2.height=h;
      ctx=c2.getContext("2d");ctx.drawImage(canvas,0,0);
    }
    return ctx.getImageData(0,0,w,h).data;
  }
  function encodeCanvasPng(canvas){
    try{return Promise.resolve(encodeRgbaPng(canvas.width,canvas.height,pixelsOf(canvas)))}catch(e){return Promise.reject(e)}
  }
  /* v4.1: guard canvas.toDataURL for PNG. Valid result -> returned unchanged. Broken result -> PNG encoded
     from the canvas pixels by us. If even that fails, the original (broken) result is returned and the
     safe writer refuses it as before. */
  var _origToDataURL=null;
  function guardedToDataURL(type,quality){
    var uri=_origToDataURL.apply(this,arguments);
    if(type&&"image/png"!==String(type).toLowerCase())return uri;
    if("string"!=typeof uri||uri.length<64||0!==uri.indexOf("data:image/png"))return uri;
    var buf,err;
    try{buf=Buffer.from(uri.slice(uri.indexOf(",")+1),"base64");err=checkBuffer(buf)}catch(e){return uri}
    if(!err)return uri;
    _stats.toDataUrlBad++;
    var t0=Date.now();
    try{
      var own=encodeRgbaPng(this.width,this.height,pixelsOf(this)),e2=checkBuffer(own);
      if(e2){log("own encoder ALSO produced a broken PNG ("+e2+") - check RAM/CPU stability ("+this.width+"x"+this.height+")");return uri}
      _stats.ownEncoder++;
      log("canvas.toDataURL returned a broken PNG ("+this.width+"x"+this.height+": "+err+") -> replaced by own encoder ("+own.length+" bytes, "+(Date.now()-t0)+" ms)");
      return"data:image/png;base64,"+own.toString("base64");
    }catch(e){log("own encoder failed ("+(e&&e.message)+"), keeping original "+this.width+"x"+this.height);return uri}
  }
  function installCanvasGuard(){
    try{
      if("undefined"==typeof HTMLCanvasElement||_origToDataURL)return!1;
      _origToDataURL=HTMLCanvasElement.prototype.toDataURL;
      HTMLCanvasElement.prototype.toDataURL=guardedToDataURL;
      return!0;
    }catch(e){log("canvas guard NOT installed: "+(e&&e.message));return!1}
  }
  /* cheap on-disk check: "valid" | "invalid" | "unknown" (unreadable right now, or a format we do not understand) | "missing" */
  function fileState(p){
    var st;
    try{st=fs().statSync(p)}catch(e){return e&&"ENOENT"===e.code?"missing":"unknown"}
    if(!st.isFile())return"unknown";
    if(st.size<32)return"invalid";
    var fd,head=Buffer.alloc(8),tail=Buffer.alloc(12);
    try{fd=fs().openSync(p,"r")}catch(e){return e&&"ENOENT"===e.code?"missing":"unknown"}
    try{fs().readSync(fd,head,0,8,0);fs().readSync(fd,tail,0,12,st.size-12)}catch(e){return"unknown"}finally{try{fs().closeSync(fd)}catch(e){}}
    if(isPng(head))return"IEND"===tail.toString("latin1",4,8)&&174===tail[8]&&66===tail[9]&&96===tail[10]&&130===tail[11]?"valid":"invalid";
    if(isJpg(head))return 255===tail[10]&&217===tail[11]?"valid":"invalid";
    return"unknown";
  }
  function fileLooksValid(p){return"valid"===fileState(p)}
  /* deep on-disk check: reads the whole file and runs the FULL check (v4: incl. image data). Result cached per (path,size,mtime). */
  function deepFileState(p){
    var s=fileState(p);if("valid"!==s)return s;
    try{
      var st=fs().statSync(p),key=st.size+":"+st.mtimeMs;
      if(_deepCache[p]===key)return"valid";
      var err=checkBuffer(fs().readFileSync(p));
      if(err){log("deep check FAILED on disk: "+err+" | "+p);return"invalid"}
      _deepCache[p]=key;return"valid";
    }catch(e){return"unknown"}
  }
  function removeQuietly(p){try{fs().unlinkSync(p);return!0}catch(e){return!1}}
  /* cache lookup used instead of fs.existsSync(): a positively broken cached tile is deleted so it gets re-rendered;
     files we cannot read right now (locked) or do not understand are left alone */
  function cachedFileIsValid(p){
    var s=deepFileState(p);
    if("valid"===s)return!0;
    if("missing"===s)return!1;
    if("unknown"===s){log("cache: keeping file that could not be verified (locked or unknown format): "+p);return!0}
    delete _deepCache[p];
    var removed=removeQuietly(p);
    log("cache: corrupt/incomplete tile "+(removed?"removed":"could NOT be removed (in use?)")+", re-rendering: "+p);
    return!1;
  }
  /* remove stale *.part temp files (older than 10 min) once per directory per session */
  function sweepDir(dir){
    if(_swept[dir])return;_swept[dir]=!0;
    try{var now=Date.now();fs().readdirSync(dir).forEach(function(f){if(/\.part$/i.test(f)){var p=pth().join(dir,f);try{if(now-fs().statSync(p).mtimeMs>6e5&&removeQuietly(p))log("swept stale temp file: "+p)}catch(e){}}})}catch(e){}
  }
  function renameWithRetry(tmp,target,attempts,cb){
    fs().rename(tmp,target,function(e){
      if(!e||attempts<=1)return cb(e);
      setTimeout(function(){renameWithRetry(tmp,target,attempts-1,cb)},80);
    });
  }
  function firstDiff(a,b){var n=Math.min(a.length,b.length);for(var i=0;i<n;i++)if(a[i]!==b[i])return{offset:i,expected:a[i].toString(16),found:b[i].toString(16)};return a.length!==b.length?{offset:n,expected:"len "+a.length,found:"len "+b.length}:null}
  /* write tmp, read it back and compare byte-for-byte; retry a few times if the bytes on disk differ from memory */
  function writeVerified(tmp,buf,attempt,cb){
    fs().writeFile(tmp,buf,function(e){
      if(e)return cb(e);
      var back;try{back=fs().readFileSync(tmp)}catch(e2){return cb(e2)}
      if(back.equals(buf))return cb(null);
      var d=firstDiff(buf,back);
      log("READ-BACK MISMATCH (attempt "+attempt+") at offset "+d.offset+": memory="+d.expected+" disk="+d.found+" | "+tmp);
      if(attempt>=3)return cb(new Error("file on disk differs from memory after 3 attempts (offset "+d.offset+")"));
      removeQuietly(tmp);
      setTimeout(function(){writeVerified(tmp,buf,attempt+1,cb)},50);
    });
  }
  /* atomic write: validate -> unique temp file -> read-back verify -> rename over target (with retries) */
  function writeBufferAtomic(buf,target){
    return new Promise(function(resolve,reject){
      var err=checkBuffer(buf);
      if(err){
        /* the image was broken BEFORE touching the disk: re-check once to tell a transient memory error from a bad render */
        var err2=checkBuffer(buf);
        log("REFUSED to write corrupt image ("+err+(err2===err?"":" | second check: "+(err2||"OK"))+"): "+target);
        return reject(new Error("refusing to write corrupt image ("+err+"): "+target));
      }
      var dir=pth().dirname(target),tmp=target+"."+process.pid+"."+Date.now()+"."+(_seq++)+".part";
      try{require("fs-extra").mkdirsSync(dir)}catch(e){}
      sweepDir(dir);
      writeVerified(tmp,buf,1,function(e){
        if(e){removeQuietly(tmp);log("write failed ("+(e.code||e.message)+"): "+target);return reject(e)}
        renameWithRetry(tmp,target,6,function(e2){
          if(!e2){delete _deepCache[target];return resolve(target)}
          removeQuietly(tmp);
          var s=fileState(target);
          if("valid"===s){log("rename blocked ("+(e2.code||e2.message)+"), existing valid file kept: "+target);return resolve(target)}
          log("rename FAILED ("+(e2.code||e2.message)+") and target is "+s+": "+target);
          reject(e2);
        });
      });
    });
  }
  function writeDataUriAtomic(dataUri,target){
    var buf;try{buf=require("data-uri-to-buffer")(dataUri)}catch(e){log("data-uri decode failed: "+(e&&e.message)+" | "+target);return Promise.reject(e)}
    return writeBufferAtomic(buf,target);
  }
  /* v4: write a merged tile from its canvas. If Chromium's PNG is broken, encode again; if that is broken too,
     encode with our own encoder. The canvas stays reserved (isBusy) until this promise settles. */
  function writeCanvasAtomic(canvas,mime,quality,firstUri,target){
    if("image/png"!==mime)return writeDataUriAtomic(firstUri,target);
    function decodeCheck(uri,attempt){
      var buf;try{buf=require("data-uri-to-buffer")(uri)}catch(e){log("data-uri decode failed (attempt "+attempt+"): "+(e&&e.message)+" | "+target);return null}
      var err=checkBuffer(buf);
      if(err){_stats.toDataUrlBad++;log("BROKEN PNG from canvas.toDataURL (attempt "+attempt+": "+err+"), re-encoding: "+target);return null}
      return buf;
    }
    var buf=decodeCheck(firstUri,1);
    if(!buf){
      var again=null;try{again=canvas.toDataURL(mime,quality)}catch(e){}
      buf=again&&decodeCheck(again,2);
      if(buf){_stats.retryOk++;log("re-encode OK on attempt 2: "+target)}
    }
    if(buf)return writeBufferAtomic(buf,target);
    return encodeCanvasPng(canvas).then(function(own){
      var err=checkBuffer(own);
      if(err){log("own encoder ALSO produced a broken PNG ("+err+") - check RAM/CPU stability: "+target);throw new Error("refusing to write corrupt image ("+err+"): "+target)}
      _stats.ownEncoder++;log("own encoder used ("+own.length+" bytes): "+target);
      return writeBufferAtomic(own,target);
    });
  }
  /* de-duplicate concurrent renders of the same target file */
  function once(key,fn){
    if(_inflight[key])return _inflight[key];
    var p=Promise.resolve().then(fn),clear=function(){delete _inflight[key]};
    _inflight[key]=p;p.then(clear,clear);return p;
  }
  function note(m){log(m)}
  var _guard=installCanvasGuard();
  try{log("loaded v4.1 (pid "+process.pid+", canvas guard "+(_guard?"on":"OFF")+")")}catch(e){}
  return{version:4.1,stats:_stats,checkBuffer:checkBuffer,encodeCanvasPng:encodeCanvasPng,fileState:fileState,deepFileState:deepFileState,fileLooksValid:fileLooksValid,cachedFileIsValid:cachedFileIsValid,writeBufferAtomic:writeBufferAtomic,writeDataUriAtomic:writeDataUriAtomic,writeCanvasAtomic:writeCanvasAtomic,once:once,note:note,logPath:logPath};
})();