// Adapter around the unchanged, hash-verified official PC renderer.
// Account cookies never enter this process; input/output use anonymous pipes.
import fs from 'node:fs';
import {webcrypto} from 'node:crypto';
import readline from 'node:readline';
import {pathToFileURL} from 'node:url';
const mod=await import(pathToFileURL(process.argv[2]).href);
globalThis.crypto ||= webcrypto;
mod.initSync({module:fs.readFileSync(process.argv[3])});
mod.set_runtime_env('prod');
class Window {constructor(){this.devicePixelRatio=1;this.crypto=webcrypto;}}
class CanvasRenderingContext2D {
 constructor(){this.draws=[];this.font='18px sans-serif';}
 measureText(text){return {width:[...text].reduce((n,c)=>n+(c.charCodeAt(0)<128?9:18),0)};}
 fillText(text,x,y){if(text)this.draws.push({text,x,y});}
 fillRect(){} save(){} restore(){} setTransform(){} beginPath(){} moveTo(){} lineTo(){} stroke(){}
}
globalThis.Window=Window;globalThis.window=new Window();globalThis.CanvasRenderingContext2D=CanvasRenderingContext2D;
console.log(JSON.stringify({ready:true}));
const rl=readline.createInterface({input:process.stdin});
for await(const line of rl){
 try {
  const req=JSON.parse(line);
  if(req.action==='sign'){
   mod.clear_buffer();mod.clear_decryption_key();mod.clear_ecdh_key();
   const publicKey=Buffer.from(mod.generate_ecdh_keypair()).toString('base64');
   const salt=Buffer.from(crypto.getRandomValues(new Uint8Array(16))).toString('base64');
   const signature=mod.sign_request_payload(publicKey,req.timestamp,salt);
   console.log(JSON.stringify({publicKey,salt,signature}));
  }else if(req.action==='render'){
   mod.derive_and_inject_session_key(Buffer.from(req.serverKey,'base64'),Buffer.from(req.salt,'base64'),req.entryId,req.timestamp,req.publicKey);
   for(const chunk of req.chunks){
    const data=Buffer.from(chunk,'base64');
    if(req.chunks.length>1){const framed=Buffer.alloc(data.length+4);framed.writeUInt32BE(data.length);data.copy(framed,4);mod.receive_encrypted_chunk(framed);}
    else mod.receive_encrypted_chunk(data);
   }
   // Large logical canvas avoids imposing the PC reader's narrow visual line wraps.
   // No raster image is allocated. Drawn rows remain ordered and blank rows are retained.
   const ctx=new CanvasRenderingContext2D();const canvas={width:1000000,height:1000000,getContext:()=>ctx};
   const linesPerPage=100000;
   const pages=mod.get_total_pages(canvas,linesPerPage,18,1.8,'sans-serif',50,0,0);
   if(pages!==1)throw Error('unexpected page count');
   mod.render_page(canvas,0,linesPerPage,18,1.8,'sans-serif','#000','#fff',50,0,0);
   const rows=new Map();
   for(const draw of ctx.draws){const row=rows.get(draw.y)||[];row.push(draw);rows.set(draw.y,row);}
   const ys=[...rows.keys()].sort((a,b)=>a-b);
   if(!ys.length)throw Error('empty render');
   let out=[],prev=null;
   for(const y of ys){
    if(prev!==null && y-prev>18*1.8*1.5)out.push('');
    out.push(rows.get(y).sort((a,b)=>a.x-b.x).map(d=>d.text).join(''));prev=y;
   }
   const content=out.join('\n').trim();
   if(!content)throw Error('empty content');
   console.log(JSON.stringify({content,rows:ys.length,draws:ctx.draws.length}));
   mod.clear_buffer();mod.clear_decryption_key();mod.clear_ecdh_key();
  }else throw Error('unknown action');
 }catch(e){console.log(JSON.stringify({error:'PC renderer operation failed'}));}
}
