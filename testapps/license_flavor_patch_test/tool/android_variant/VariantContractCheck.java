package dev.shorebird.e2e.compilecheck;

import java.io.IOException;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.atomic.AtomicInteger;
import org.json.*;

/** Host JVM test of the SAME authorization gate used before real cached reads. */
public final class VariantContractCheck {
  interface Action { void run() throws Exception; }
  static int checks;
  static void rejects(Action action) throws Exception {
    try { action.run(); } catch(Exception expected) { checks++; return; }
    throw new AssertionError("Expected closed authorization gate");
  }
  static JSONObject clone(JSONObject value) throws Exception {
    return (JSONObject)new PatchKeyProvider.StrictJson(value.toString()).parse();
  }
  static void noCachedRelease(String id,String aad,String context,JSONArray allowed,String release) throws Exception {
    AtomicInteger reads=new AtomicInteger();
    rejects(()->PatchKeyProvider.authorizedKey(id,aad,context,allowed,release,
        ()->{reads.incrementAndGet();return new byte[32];},identity->{}));
    if(reads.get()!=0) throw new AssertionError("Unauthorized context reached cached key source");
    checks++;
  }
  public static void main(String[] args) throws Exception {
    JSONObject fixture=(JSONObject)new PatchKeyProvider.StrictJson(Files.readString(Path.of(args[0]))).parse();
    JSONObject context=fixture.getJSONObject("context"); JSONArray allowed=new JSONArray().put(context);
    String aad=fixture.getString("aad"),id=fixture.getString("key_id"),release="17";
    AtomicInteger reads=new AtomicInteger(); List<JSONObject> receipts=new ArrayList<>();
    byte[] key=PatchKeyProvider.authorizedKey(id,aad,context.toString(),allowed,release,
        ()->{reads.incrementAndGet();byte[] cached=new byte[32];Arrays.fill(cached,(byte)0x42);return cached;},receipts::add);
    if(key.length!=32 || key[0]!=0x42 || reads.get()!=1 || receipts.size()!=1 ||
        !receipts.get(0).getString("digest").equals(context.getString("selection_digest"))) throw new AssertionError();
    Arrays.fill(key,(byte)0); checks++;
    for(String bad:new String[]{"", "short", "invalid/key-id___"}) noCachedRelease(bad,aad,context.toString(),allowed,release);
    for(String bad:new String[]{aad+"\n",aad.replace("platform=android","platform=linux"),
        aad.replace("arch=aarch64","arch=x86_64"),aad.replace("device_id_hash=cc","device_id_hash=dd"),
        aad.replace("patch_hash=aa","patch_hash=bb"),aad.replace("release_id=17","release_id=18")})
      noCachedRelease(id,bad,context.toString(),allowed,release);
    noCachedRelease(id,aad,context.toString(),new JSONArray(),release);
    noCachedRelease(id,aad,context.toString().replaceFirst("\\{","{\"version\":2,"),allowed,release);
    noCachedRelease(id,aad,context.toString(),allowed,"18");
    for(String name:new String[]{"app_id","release_version","platform","arch","client_id_hash","base_artifact_hash","runtime_profile","host_abi_hash"}) {
      JSONObject changed=clone(context),scope=changed.getJSONObject("selection").getJSONObject("scope");
      scope.put(name,name.equals("host_abi_hash")?"a".repeat(64):"changed");
      noCachedRelease(id,aad,changed.toString(),new JSONArray().put(changed),release);
    }
    for(String name:new String[]{"kind","patch_number","artifact_hash","encryption","artifact_key_id"}) {
      JSONObject changed=clone(context); changed.getJSONObject("selection").getJSONObject("target").put(name,"changed");
      noCachedRelease(id,aad,changed.toString(),new JSONArray().put(changed),release);
    }
    for(String name:new String[]{"epoch","channel","variant_id","license_type","entitlement_revision","not_before","offline_valid_until"}) {
      JSONObject changed=clone(context); changed.getJSONObject("selection").put(name,
          name.equals("not_before")?"253402300799":name.equals("offline_valid_until")?"1":"changed");
      noCachedRelease(id,aad,changed.toString(),new JSONArray().put(changed),release);
    }
    String fractional=context.toString().replace("\"version\":2", "\"version\":2.0");
    noCachedRelease(id,aad,fractional,allowed,release);
    byte[] cached=new byte[32]; Arrays.fill(cached,(byte)0x42);
    rejects(()->PatchKeyProvider.authorizedKey(id,aad,context.toString(),allowed,release,()->cached,
        identity->{throw new IOException();}));
    for(byte value:cached) if(value!=0) throw new AssertionError("Failed receipt leaked key bytes"); checks++;
    if(PatchKeyProvider.recordName(id,aad).equals(PatchKeyProvider.recordName("AAAAAAAAAAAAAAAA",aad)) ||
        PatchKeyProvider.recordName(id,aad).equals(PatchKeyProvider.recordName(id,aad.replace("transport_hash=bb","transport_hash=cc"))) ||
        Arrays.equals(PatchKeyProvider.wrappingAad(id,aad),PatchKeyProvider.wrappingAad("AAAAAAAAAAAAAAAA",aad))) throw new AssertionError();
    checks++;
    System.out.println("PASS: "+checks+" native V2 verified-context/cache-gate checks; Android runtime unrun");
  }
}
