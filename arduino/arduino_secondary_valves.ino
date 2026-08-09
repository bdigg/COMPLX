// Secondary microcontroller sketch for valves 7, 8, 9 only.
// Command format matches existing Python code:
//   <A,servoNumber,angle>
// Supported servo numbers:
//   7 -> pin 10
//   8 -> pin 9
//   9 -> pin 8

#include <Servo.h>

const byte NUM_CHARS = 32;
char receivedChars[NUM_CHARS];
bool newData = false;

char messageFromPC = 0;
float floatFromPC1 = 0.0;
float floatFromPC2 = 0.0;
float floatFromPC3 = 0.0;

Servo valve7;
Servo valve8;
Servo valve9;

int pos7 = 80;
int pos8 = 80;
int pos9 = 80;

const int SERVO_STEP = 1;
const int SERVO_DELAY_MS = 15;

void moveServoToAngle(Servo &s, int targetAngle, int &currentAngle) {
  targetAngle = constrain(targetAngle, 0, 180);

  if (targetAngle > currentAngle) {
    for (int a = currentAngle; a <= targetAngle; a += SERVO_STEP) {
      s.write(a);
      delay(SERVO_DELAY_MS);
    }
  } else {
    for (int a = currentAngle; a >= targetAngle; a -= SERVO_STEP) {
      s.write(a);
      delay(SERVO_DELAY_MS);
    }
  }

  s.write(targetAngle);
  currentAngle = targetAngle;
}

void setValveAngle(int servoNumber, int angle) {
  switch (servoNumber) {
    case 7: moveServoToAngle(valve7, angle, pos7); break;
    case 8: moveServoToAngle(valve8, angle, pos8); break;
    case 9: moveServoToAngle(valve9, angle, pos9); break;
    default:
      Serial.println("Bad Servo #");
      return;
  }
  Serial.println("OK");
}

void recvWithStartEndMarkers() {
  static bool recvInProgress = false;
  static byte ndx = 0;
  const char startMarker = '<';
  const char endMarker = '>';
  char rc;

  while (Serial.available() > 0 && !newData) {
    rc = Serial.read();

    if (recvInProgress) {
      if (rc != endMarker) {
        receivedChars[ndx] = rc;
        ndx++;
        if (ndx >= NUM_CHARS) ndx = NUM_CHARS - 1;
      } else {
        receivedChars[ndx] = '\0';
        recvInProgress = false;
        ndx = 0;
        newData = true;
      }
    } else if (rc == startMarker) {
      recvInProgress = true;
    }
  }
}

void parseData() {
  messageFromPC = receivedChars[0];

  char *tok;
  tok = strtok(receivedChars + 1, ",");
  floatFromPC1 = (tok != NULL) ? atof(tok) : 0;

  tok = strtok(NULL, ",");
  floatFromPC2 = (tok != NULL) ? atof(tok) : 0;

  tok = strtok(NULL, ",");
  floatFromPC3 = (tok != NULL) ? atof(tok) : 0;
}

void showParsedData() {
  if (messageFromPC == 'A') {
    setValveAngle((int)floatFromPC1, (int)floatFromPC2);
  } else {
    Serial.println("?");
  }
}

void setup() {
  valve7.attach(10);
  valve8.attach(9);
  valve9.attach(8);

  valve7.write(pos7);
  valve8.write(pos8);
  valve9.write(pos9);

  Serial.begin(115200);
  while (!Serial) { delay(1); }
  Serial.println("Secondary valves ready");
}

void loop() {
  recvWithStartEndMarkers();
  if (newData) {
    parseData();
    showParsedData();
    newData = false;
  }
}
