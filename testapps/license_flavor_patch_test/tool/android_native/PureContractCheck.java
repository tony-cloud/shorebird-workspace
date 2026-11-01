package dev.shorebird.e2e.compilecheck;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.concurrent.atomic.AtomicReference;

/** Runs on the host JVM; never invokes Android platform stubs. */
public final class PureContractCheck {
  interface Action { void run() throws Exception; }
  static int checks;
  static void accepts(Action a) throws Exception { a.run(); checks++; }
  static void rejects(Action a) throws Exception {
    try { a.run(); } catch(Exception expected) { checks++; return; }
    throw new AssertionError("Expected fail-closed rejection");
  }
  static void http(String response,boolean valid) throws Exception {
    AtomicReference<Throwable> failure=new AtomicReference<>();
    try(ServerSocket server=new ServerSocket(0,1,InetAddress.getByName("127.0.0.1"))) {
      server.setSoTimeout(5000);
      Thread peer=new Thread(()->{
        try(Socket socket=server.accept()) {
          socket.setSoTimeout(5000); InputStream in=socket.getInputStream(); int matched=0;
          byte[] end={13,10,13,10};
          while(matched<4) { int c=in.read(); if(c<0) throw new IOException(); matched=c==end[matched]?matched+1:0; }
          if(in.read()!='{'||in.read()!='}') throw new IOException();
          socket.getOutputStream().write(response.getBytes(StandardCharsets.US_ASCII));
        } catch(Throwable e) { failure.set(e); }
      });
      peer.setDaemon(true); peer.start();
      Action request=()->PatchKeyProvider.post("http://127.0.0.1:"+server.getLocalPort()+"/key","key",new byte[]{'{','}'},"secret-token",64,false);
      if(valid) accepts(request); else rejects(request);
      peer.join(6000); if(peer.isAlive()||failure.get()!=null) throw new AssertionError("HTTP peer failed",failure.get());
    }
  }
  public static void main(String[] ignored) throws Exception {
    String aad="open-aot-patch-delivery-v2\napp_id=7cd314d0-8ace-46a2-b21b-81a92bcd0738\nrelease_id=17\npatch_number=3\npatch_hash="+"a".repeat(64)+"\ntransport_hash="+"b".repeat(64)+"\nplatform=android\narch=aarch64\ndevice_id_hash="+"c".repeat(64);
    accepts(()->PatchKeyProvider.validateContext("sQlTbnI3MlPc8POe",aad));
    for(String invalid:new String[]{aad+"\n",aad.replace("release_id=17","release_id=017"),aad.replace("arch=aarch64","arch=arm64"),aad.replace("platform=android","platform=linux"),aad.replace("app_id=7cd","app_id=7CD"),aad.replace("patch_hash=aaaa","patch_hash=AAAA"),aad.replace("release_id=17","patch_number=17"),aad.replace("aarch64","aarch64\r")})
      rejects(()->PatchKeyProvider.validateContext("sQlTbnI3MlPc8POe",invalid));
    for(String id:new String[]{"", "x", "sQlTbnI3MlPc8PO=", "sQlTbnI3MlPc8PO/"}) rejects(()->PatchKeyProvider.validateContext(id,aad));
    accepts(()->PatchKeyProvider.endpoint("http://127.0.0.1:65535/key","key"));
    for(String invalid:new String[]{"http://127.0.0.1:0/key","http://127.0.0.1:01/key","http://127.0.0.1:65536/key","https://127.0.0.1:1/key","http://localhost:1/key","http://user@127.0.0.1:1/key","http://127.0.0.1:1/key?x","http://127.0.0.1:1/key#x","http://127.0.0.1:1/native"}) rejects(()->PatchKeyProvider.endpoint(invalid,"key"));
    rejects(()->PatchKeyProvider.utf8("\ud800")); rejects(()->PatchKeyProvider.utf8("a\0b"));
    rejects(()->PatchKeyProvider.decode(new byte[]{(byte)0xff})); rejects(()->PatchKeyProvider.utf8("x".repeat(32769)));
    accepts(()->PatchKeyProvider.utf8("valid-\u00e9"));
    byte[] key=PatchKeyProvider.decodeKeyResponse("ab".repeat(32).getBytes(StandardCharsets.US_ASCII));
    if(key.length!=32||key[0]!=(byte)0xab) throw new AssertionError(); Arrays.fill(key,(byte)0); checks++;
    for(String invalid:new String[]{"ab".repeat(31),"AB".repeat(32),"ab".repeat(32)+"\n"}) rejects(()->PatchKeyProvider.decodeKeyResponse(invalid.getBytes(StandardCharsets.US_ASCII)));
    http("HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok",true);
    http("HTTP/1.0 302 Found\r\nContent-Length: 0\r\n\r\n",false);
    http("HTTP/1.0 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\nok",false);
    http("HTTP/1.0 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",false);
    http("HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nokextra",false);
    http("HTTP/1.0 200 OK\r\nContent-Length: 3\r\n\r\nok",false);
    byte[] hmacKey=new byte[20]; Arrays.fill(hmacKey,(byte)0x0b);
    if(!PatchKeyProvider.hex(PatchKeyProvider.hmac(hmacKey,"Hi There".getBytes(StandardCharsets.US_ASCII))).equals("b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7")) throw new AssertionError(); checks++;
    if(ignored.length>0) {
      accepts(()->new PatchKeyProvider.StrictJson("{\"x\":[1,true,false,null,\"valid\\u00e9\"],\"empty\":{}}").parse());
      for(String invalid:new String[]{"{x:1}","{\"x\":1,}","[1,]","{\"x\":1,\"x\":2}","{\"a\":{\"x\":1,\"x\":2}}","{\"x\":01}","{\"x\":NaN}","{\"x\":\"\\x41\"}","{\"x\":\"\\ud800\"}","{}garbage"}) rejects(()->new PatchKeyProvider.StrictJson(invalid).parse());
    }
    System.out.println("PASS: "+checks+" pure validator/HTTP/crypto"+(ignored.length>0?"/strict-JSON":"")+" checks (Android platform behavior unrun)");
  }
}
