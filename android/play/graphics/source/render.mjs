// Run from the repository root: node android/play/graphics/source/render.mjs
// Uses the UI's installed Playwright. Original screenshots remain pixel-identical;
// their opaque PNG encoding is normalized to Google's required 24-bit RGB.
import { chromium } from '../../../../ui/node_modules/playwright/index.mjs';
import { readFile, writeFile, readdir } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import { deflateSync } from 'node:zlib';
const source = path.dirname(fileURLToPath(import.meta.url));
const out = path.dirname(source);
const root = path.resolve(out, '../../..');
const browser = await chromium.launch({headless:true});
const page = await browser.newPage({viewport:{width:1024,height:500},deviceScaleFactor:1});
const crc = buf => { let c=0xffffffff; for(const byte of buf){c^=byte;for(let k=0;k<8;k++)c=(c>>>1)^((c&1)?0xedb88320:0);}return (c^0xffffffff)>>>0; };
function chunk(type,data){const t=Buffer.from(type),n=Buffer.alloc(4),c=Buffer.alloc(4);n.writeUInt32BE(data.length);c.writeUInt32BE(crc(Buffer.concat([t,data])));return Buffer.concat([n,t,data,c]);}
function png(width,height,rgba,alpha=false){
 const channels=alpha?4:3,raw=Buffer.alloc(height*(1+width*channels));
 for(let y=0;y<height;y++)for(let x=0;x<width;x++)for(let c=0;c<channels;c++)raw[y*(1+width*channels)+1+x*channels+c]=rgba[(y*width+x)*4+c];
 const header=Buffer.alloc(13);header.writeUInt32BE(width);header.writeUInt32BE(height,4);header[8]=8;header[9]=alpha?6:2;
 return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',header),chunk('IDAT',deflateSync(raw,{level:9})),chunk('IEND',Buffer.alloc(0))]);
}
async function normalize(input,output,width,height,alpha=false){
 const url='data:image/png;base64,'+(await readFile(input)).toString('base64');
 const pixels=await page.evaluate(async({url,width,height})=>{const im=new Image();im.src=url;await im.decode();const c=document.createElement('canvas');c.width=width||im.width;c.height=height||im.height;const ctx=c.getContext('2d');ctx.drawImage(im,0,0,c.width,c.height);const data=ctx.getImageData(0,0,c.width,c.height).data;let s='';for(let i=0;i<data.length;i+=32768)s+=String.fromCharCode(...data.subarray(i,i+32768));return {w:c.width,h:c.height,base64:btoa(s)};},{url,width,height});
 await writeFile(output,png(pixels.w,pixels.h,Buffer.from(pixels.base64,'base64'),alpha));
}
await page.goto(pathToFileURL(path.join(source,'feature.html')).href);
await page.evaluate(()=>document.fonts.ready);
await page.screenshot({path:path.join(out,'feature-graphic.png')});
await normalize(path.join(out,'feature-graphic.png'),path.join(out,'feature-graphic.png'));
await normalize(path.join(root,'android/app/src/main/res/drawable-nodpi/app_icon.png'),path.join(out,'app-icon.png'),512,512,true);
const groups=['phone','tablet-7-inch','tablet-10-inch'];
for(const group of groups)for(const file of (await readdir(path.join(out,group))).filter(f=>f.endsWith('.png'))){const p=path.join(out,group,file);await normalize(p,p);}
const uri=async p=>'data:image/png;base64,'+(await readFile(p)).toString('base64');
let html=`<!doctype html><meta charset="utf-8"><style>*{box-sizing:border-box}body{margin:0;padding:44px;background:#eaf0f9;color:#061b49;font:16px Arial}h1{margin:0 0 8px;font-size:32px}p{margin:0 0 28px;color:#486385}.top{display:flex;gap:30px;align-items:center}.icon{width:180px;border-radius:36px}.feature{width:717px;border-radius:16px}.row{display:flex;gap:18px}figure{margin:0;flex:1;min-width:0}figure img{width:100%;border:1px solid #c9d5e7;border-radius:8px}figcaption{font-size:12px;margin-top:8px}h2{margin:30px 0 14px;font-size:20px}</style><h1>Hardcharger™ · Google Play graphics</h1><p>Actual Android Play build 1501 · Synthetic sample data · Phone + 7-inch + 10-inch tablet</p><div class="top"><img class="icon" src="${await uri(path.join(out,'app-icon.png'))}"><img class="feature" src="${await uri(path.join(out,'feature-graphic.png'))}"></div>`;
for(const group of groups){html+=`<h2>${group.replaceAll('-',' ')}</h2><div class="row">`;for(const file of (await readdir(path.join(out,group))).filter(f=>f.endsWith('.png')).sort())html+=`<figure><img src="${await uri(path.join(out,group,file))}"><figcaption>${file.replace('.png','').replaceAll('-',' ')}</figcaption></figure>`;html+='</div>';}
await writeFile(path.join(out,'preview.html'),html);
await page.setViewportSize({width:1200,height:900});
await page.setContent(html);await page.evaluate(()=>Promise.all([...document.images].map(i=>i.decode())));
await page.screenshot({path:path.join(out,'preview.png'),fullPage:true});
await browser.close();
console.log('Rendered icon, feature graphic and preview; normalized screenshot PNGs to 24-bit RGB.');
