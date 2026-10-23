package com.pianotasks.app;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * A small JSON reader/writer.
 *
 * Android ships org.json, but using it would tie this class (and therefore the
 * API client) to the Android runtime and make it impossible to exercise on a
 * plain JVM. Hand-rolling ~150 lines keeps the whole networking layer portable
 * and the APK dependency-free.
 */
public final class Json {

    public static class JsonException extends RuntimeException {
        public JsonException(String message) { super(message); }
    }

    private final String src;
    private int pos;

    private Json(String src) {
        this.src = src;
    }

    // ---------------------------------------------------------------- reading

    public static Object parse(String text) {
        if (text == null) return null;
        Json p = new Json(text);
        p.ws();
        Object value = p.value();
        p.ws();
        if (p.pos != p.src.length()) {
            throw new JsonException("trailing characters at offset " + p.pos);
        }
        return value;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> obj(Object value) {
        if (value == null) return new LinkedHashMap<String, Object>();
        if (!(value instanceof Map)) throw new JsonException("expected a JSON object");
        return (Map<String, Object>) value;
    }

    @SuppressWarnings("unchecked")
    public static List<Object> arr(Object value) {
        if (value == null) return new ArrayList<Object>();
        if (!(value instanceof List)) throw new JsonException("expected a JSON array");
        return (List<Object>) value;
    }

    public static boolean bool(Object value) {
        return value instanceof Boolean && (Boolean) value;
    }

    public static long num(Object value) {
        if (value instanceof Number) return ((Number) value).longValue();
        if (value == null) return 0L;
        try {
            return (long) Double.parseDouble(String.valueOf(value));
        } catch (NumberFormatException e) {
            return 0L;
        }
    }

    public static double dbl(Object value) {
        if (value instanceof Number) return ((Number) value).doubleValue();
        if (value == null) return 0d;
        try {
            return Double.parseDouble(String.valueOf(value));
        } catch (NumberFormatException e) {
            return 0d;
        }
    }

    public static String str(Object value) {
        return value == null ? null : String.valueOf(value);
    }

    private void ws() {
        while (pos < src.length() && Character.isWhitespace(src.charAt(pos))) pos++;
    }

    private Object value() {
        if (pos >= src.length()) throw new JsonException("unexpected end of input");
        char c = src.charAt(pos);
        switch (c) {
            case '{': return object();
            case '[': return array();
            case '"': return string();
            case 't': expect("true"); return Boolean.TRUE;
            case 'f': expect("false"); return Boolean.FALSE;
            case 'n': expect("null"); return null;
            default: return number();
        }
    }

    private Map<String, Object> object() {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        pos++;                       // '{'
        ws();
        if (peek() == '}') { pos++; return out; }
        while (true) {
            ws();
            if (peek() != '"') throw new JsonException("expected a key at offset " + pos);
            String key = string();
            ws();
            if (peek() != ':') throw new JsonException("expected ':' at offset " + pos);
            pos++;
            ws();
            out.put(key, value());
            ws();
            char c = peek();
            if (c == ',') { pos++; continue; }
            if (c == '}') { pos++; return out; }
            throw new JsonException("expected ',' or '}' at offset " + pos);
        }
    }

    private List<Object> array() {
        List<Object> out = new ArrayList<Object>();
        pos++;                       // '['
        ws();
        if (peek() == ']') { pos++; return out; }
        while (true) {
            ws();
            out.add(value());
            ws();
            char c = peek();
            if (c == ',') { pos++; continue; }
            if (c == ']') { pos++; return out; }
            throw new JsonException("expected ',' or ']' at offset " + pos);
        }
    }

    private String string() {
        StringBuilder sb = new StringBuilder();
        pos++;                       // opening quote
        while (true) {
            if (pos >= src.length()) throw new JsonException("unterminated string");
            char c = src.charAt(pos++);
            if (c == '"') return sb.toString();
            if (c != '\\') { sb.append(c); continue; }
            if (pos >= src.length()) throw new JsonException("unterminated escape");
            char e = src.charAt(pos++);
            switch (e) {
                case '"':  sb.append('"');  break;
                case '\\': sb.append('\\'); break;
                case '/':  sb.append('/');  break;
                case 'b':  sb.append('\b'); break;
                case 'f':  sb.append('\f'); break;
                case 'n':  sb.append('\n'); break;
                case 'r':  sb.append('\r'); break;
                case 't':  sb.append('\t'); break;
                case 'u':
                    if (pos + 4 > src.length()) throw new JsonException("truncated \\u escape");
                    sb.append((char) Integer.parseInt(src.substring(pos, pos + 4), 16));
                    pos += 4;
                    break;
                default: throw new JsonException("bad escape \\" + e);
            }
        }
    }

    private Object number() {
        int start = pos;
        while (pos < src.length()) {
            char c = src.charAt(pos);
            if ((c >= '0' && c <= '9') || c == '-' || c == '+' || c == '.'
                    || c == 'e' || c == 'E') {
                pos++;
            } else {
                break;
            }
        }
        if (start == pos) throw new JsonException("unexpected character '" + src.charAt(pos) + "'");
        try {
            return Double.valueOf(Double.parseDouble(src.substring(start, pos)));
        } catch (NumberFormatException e) {
            throw new JsonException("bad number '" + src.substring(start, pos) + "'");
        }
    }

    private char peek() {
        if (pos >= src.length()) throw new JsonException("unexpected end of input");
        return src.charAt(pos);
    }

    private void expect(String literal) {
        if (!src.startsWith(literal, pos)) {
            throw new JsonException("expected '" + literal + "' at offset " + pos);
        }
        pos += literal.length();
    }

    // ---------------------------------------------------------------- writing

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value);
        return sb.toString();
    }

    private static void writeValue(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String) {
            writeString(sb, (String) value);
        } else if (value instanceof Boolean || value instanceof Number) {
            sb.append(value.toString());
        } else if (value instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> entry : ((Map<?, ?>) value).entrySet()) {
                if (!first) sb.append(',');
                first = false;
                writeString(sb, String.valueOf(entry.getKey()));
                sb.append(':');
                writeValue(sb, entry.getValue());
            }
            sb.append('}');
        } else if (value instanceof Iterable) {
            sb.append('[');
            boolean first = true;
            for (Object item : (Iterable<?>) value) {
                if (!first) sb.append(',');
                first = false;
                writeValue(sb, item);
            }
            sb.append(']');
        } else {
            writeString(sb, String.valueOf(value));
        }
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':  sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n");  break;
                case '\r': sb.append("\\r");  break;
                case '\t': sb.append("\\t");  break;
                case '\b': sb.append("\\b");  break;
                case '\f': sb.append("\\f");  break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        sb.append('"');
    }
}