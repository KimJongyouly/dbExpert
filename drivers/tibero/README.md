# Tibero JDBC Driver (직접 준비 필요)

`server/database/tibero.py`는 JPype로 JVM을 띄운 뒤 이 디렉터리의
`tibero6-jdbc.jar`를 classpath에 올려 `java.sql.DriverManager`로 직접
접속한다(Tibero에는 순정 Python DB-API 드라이버가 없기 때문).

## ⚠️ jar는 저장소에 포함되지 않는다

Tibero JDBC 드라이버는 TmaxData가 배포하는 소프트웨어라 이 저장소에
번들하지 않는다(`.gitignore`의 `drivers/tibero/*.jar`). Tibero를 쓰려면
사용자가 직접 준비해야 한다.

1. 접속 대상 Tibero 서버 버전에 맞는 JDBC 드라이버를 구한다. 보통 Tibero
   서버 설치 디렉터리의 `client/lib/jar/` 아래에 있거나, TmaxData
   기술지원/다운로드 사이트에서 받을 수 있다.
2. 파일을 **정확히** `drivers/tibero/tibero6-jdbc.jar` 경로에 둔다.
   파일명이 다르면 그대로 두고 `server/database/tibero.py`의
   `_DRIVER_JAR_PATH`를 그 파일로 바꿔도 된다.
3. 호스트에 JVM(Java Runtime)이 설치되어 있어야 한다(`java -version`).
   `uv sync --extra tibero`로 설치되는 `JPype1`은 Java 브릿지일 뿐이라
   JVM을 대신하지 못한다.

jar가 없으면 Tibero Connection만 `DBConnectionError`("Tibero JDBC Driver를
찾을 수 없습니다")로 실패하고, 다른 Engine Connection은 영향받지 않는다.

## 검증된 조합

이 Adapter가 실제로 접속을 **확인한** 조합은 다음 하나뿐이다.

| 항목 | 값 |
|---|---|
| 드라이버 파일 | `tibero6-jdbc.jar` |
| 드라이버 Manifest | `Specification-Version: 6.0.166454` (JDK 1.6용, 2019-09 빌드) |
| 대상 서버 | **Tibero 5.0** |

파일명이 "tibero6"이지만 접속을 확인한 서버는 Tibero **5.0**이다. JDBC
드라이버 버전과 DB 서버 버전 표기가 다를 수 있으니 이름만 보고 서버
버전을 추정하지 말 것. 다른 조합에서는 연결 실패, 프로토콜 불일치, 일부
타입 처리 오류가 있을 수 있다. `TiberoAdapter`는 최초 연결 시 이 사실을
`RuntimeWarning`과 `ping()` 응답의 `driver_warning` 필드로 알려주지만,
실제 호환 여부는 대상 서버에서 직접 확인해야 한다.

## 운영 환경 요구사항

JVM은 pip 의존성과 별개의 운영체제 설치물이다. "Plugin 설치 후 수동 설정
최소화" 원칙(Doc/00_개발요건사항.md §41)과 충돌하는 지점이므로, Tibero를
쓰는 환경에서는 JVM 설치 여부와 jar 준비를 사전에 확인해야 한다.
