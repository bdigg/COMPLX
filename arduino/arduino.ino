// Adapted from Niall McIntyre 2023 (Adapted from Nick Brooks)
// Updated: removed flow-switch code; added extra servos on pins 11,12,13,17,18,19
#include <ezButton.h>
#include <Servo.h>

const byte numChars = 32;
char receivedChars[numChars];

char messageFromPC = 0;
float floatFromPC1 = 0.0;
float floatFromPC2 = 0.0;
float floatFromPC3 = 0.0;
boolean newData = false;

// Stepper + syringe pins
const int dirPin1 = 2;
const int stepPin1 = 3;
const int dirPin2 = 5;
const int stepPin2 = 6;
const int dirPin3 = 8;
const int stepPin3 = 9;

const int servopin = 14;     // KEEP CURRENT SERVO (unchanged behavior)
const int syrdirpin = 15;
const int syrsteppin = 16;

const int motorSpeed = 1000;

// Limit switches
const int ls1 = 4;
const int ls2 = 7;
const int ls3 = 10;

ezButton limitSwitch1(ls1);
ezButton limitSwitch2(ls2);
ezButton limitSwitch3(ls3);

// ---------------- Servo setup ----------------

// Current servo (UNCHANGED behavior: still uses 0/1 sweep)
Servo myservo;
int pos = 0;

// Extra servos with angle control on pins: 11,12,13,17,18,19
const int extraServoPin1 = 11;
const int extraServoPin2 = 12;
const int extraServoPin3 = 13;
const int extraServoPin4 = 17;
const int extraServoPin5 = 18;
const int extraServoPin6 = 19;

Servo extraServo1, extraServo2, extraServo3, extraServo4, extraServo5, extraServo6;
// Power-up defaults:
// - Valves 1-3 closed to dobot (40 deg)
// - Valves 4-6 keep previous neutral default (80 deg)
int extraPos1 = 40, extraPos2 = 40, extraPos3 = 40, extraPos4 = 80, extraPos5 = 80, extraPos6 = 80;

// ---------------- Motor functions ----------------

void setMotorHorzDirection(int value) {
  digitalWrite(dirPin1, value);
  Serial.println("Turnt1");
}

void setMotorVertDirection(int value) {
  digitalWrite(dirPin3, value);
  if (value == 0) value = 1;
  else value = 0;
  digitalWrite(dirPin2, value);
  Serial.println("Turnt2");
}

void SyringeMove(int direction, int distance, int wait) {
  digitalWrite(syrdirpin, direction);
  for (int x = 0; x < distance; x++) {
    digitalWrite(syrsteppin, HIGH);
    delayMicroseconds(wait);
    digitalWrite(syrsteppin, LOW);
    delayMicroseconds(wait);
  }
  Serial.println("Moved Syringe");
}

void setDistanceHorz(int distance) {
  for (int x = 0; x < distance; x++) {
    digitalWrite(stepPin1, HIGH);
    delayMicroseconds(motorSpeed);
    digitalWrite(stepPin1, LOW);
    delayMicroseconds(motorSpeed);
  }
  Serial.println("Moved1");
}

void setDistanceHorzLS(int distance, int speed) {
  for (int x = 0; x < distance; x++) {
    digitalWrite(stepPin1, HIGH);
    delayMicroseconds(speed);
    digitalWrite(stepPin1, LOW);
    delayMicroseconds(speed);
  }
}

void setDistanceVert(int distance) {
  for (int x = 0; x < distance; x++) {
    digitalWrite(stepPin2, HIGH);
    delayMicroseconds(motorSpeed);
    digitalWrite(stepPin2, LOW);
    delayMicroseconds(motorSpeed);

    digitalWrite(stepPin3, HIGH);
    delayMicroseconds(motorSpeed);
    digitalWrite(stepPin3, LOW);
    delayMicroseconds(motorSpeed);
  }
  Serial.println("Moved2");
}

void setDistanceVertLeft_LS(int distance, int speed) {
  for (int x = 0; x < distance; x++) {
    digitalWrite(stepPin2, HIGH);
    delayMicroseconds(speed);
    digitalWrite(stepPin2, LOW);
    delayMicroseconds(speed);
  }
}

void setDistanceVertRight_LS(int distance, int speed) {
  for (int x = 0; x < distance; x++) {
    digitalWrite(stepPin3, HIGH);
    delayMicroseconds(speed);
    digitalWrite(stepPin3, LOW);
    delayMicroseconds(speed);
  }
}

void setMotorHorzDirectionLS(int value) {
  digitalWrite(dirPin1, value);
}

void setMotorVertDirectionLeft_LS(int value) {
  if (value == 0) value = 1;
  else value = 0;
  digitalWrite(dirPin2, value);
}

void setMotorVertDirectionRight_LS(int value) {
  digitalWrite(dirPin3, value);
}

void setDistanceBoth(int distance1, int distance2) {
  int steps1 = abs(distance1);
  int steps2 = abs(distance2);

  for (int x = 0; x < max(steps1, steps2); x++) {
    if (x < steps1) {
      digitalWrite(stepPin1, HIGH);
      delayMicroseconds(motorSpeed);
      digitalWrite(stepPin1, LOW);
      delayMicroseconds(motorSpeed);
    }
    if (x < steps2) {
      digitalWrite(stepPin2, HIGH);
      delayMicroseconds(motorSpeed);
      digitalWrite(stepPin2, LOW);
      delayMicroseconds(motorSpeed);

      digitalWrite(stepPin3, HIGH);
      delayMicroseconds(motorSpeed);
      digitalWrite(stepPin3, LOW);
      delayMicroseconds(motorSpeed);
    }
  }
  Serial.println("Moved Both");
}

// ---------------- Homing ----------------

void Homing() {
  bool isStopped1 = false;
  bool isStopped2 = false;
  bool isStopped3 = false;
  bool isStopped4 = false;
  bool isStopped5 = false;
  bool isStopped6 = false;

  while (!isStopped1) {
    limitSwitch1.loop();

    setMotorHorzDirectionLS(1);
    setMotorVertDirectionRight_LS(1);
    setMotorVertDirectionLeft_LS(1);

    setDistanceHorzLS(1, 1000);

    if (limitSwitch1.isPressed()) isStopped1 = true;
  }

  if (isStopped1) {
    delay(1000);
    setMotorHorzDirectionLS(0);
    setDistanceHorzLS(50, 1000);
    setMotorHorzDirectionLS(1);
    delay(1000);

    while (!isStopped2) {
      limitSwitch1.loop();
      setDistanceHorzLS(1, 1000);
      delay(100);
      if (limitSwitch1.isPressed()) isStopped2 = true;
    }
  }

  if (isStopped2) {
    delay(1000);

    while (!isStopped3 || !isStopped5) {
      limitSwitch2.loop();
      limitSwitch3.loop();

      if (!isStopped3) {
        setDistanceVertLeft_LS(1, 1000);
        if (limitSwitch2.isPressed()) isStopped3 = true;
      }
      if (!isStopped5) {
        setDistanceVertRight_LS(1, 1000);
        if (limitSwitch3.isPressed()) isStopped5 = true;
      }
    }

    if (isStopped3 || isStopped5) {
      delay(1000);
      setMotorVertDirectionLeft_LS(0);
      setMotorVertDirectionRight_LS(0);
      setDistanceVertLeft_LS(100, 1000);
      setDistanceVertRight_LS(100, 1000);
      setMotorVertDirectionLeft_LS(1);
      setMotorVertDirectionRight_LS(1);
      delay(1000);

      while (!isStopped4 || !isStopped6) {
        limitSwitch2.loop();
        limitSwitch3.loop();
        delay(100);

        if (!isStopped4) {
          setDistanceVertLeft_LS(1, 1000);
          if (limitSwitch2.isPressed()) isStopped4 = true;
        }
        if (!isStopped6) {
          setDistanceVertRight_LS(1, 1000);
          if (limitSwitch3.isPressed()) isStopped6 = true;
        }
      }
    }
  }
}

// ---------------- Current servo (UNCHANGED) ----------------

void servo(int value) {
  if (value == 0) {
    for (pos = 0; pos <= 69; pos += 1) {
      myservo.write(pos);
      delay(15);
    }
  }
  if (value == 1) {
    for (pos = 69; pos >= 0; pos -= 1) {
      myservo.write(pos);
      delay(15);
    }
  }
}

// ---------------- New servo angle control (for extra servos) ----------------

const int SERVO_STEP = 1;      // degrees per step
const int SERVO_DELAY_MS = 15; // delay between steps (ms)

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

// servoNumber: 1..6 maps to pins: 11,12,13,17,18,19
void setExtraServoAngle(int servoNumber, int angle) {
  switch (servoNumber) {
    case 1: moveServoToAngle(extraServo1, angle, extraPos1); break; // 11
    case 2: moveServoToAngle(extraServo2, angle, extraPos2); break; // 12
    case 3: moveServoToAngle(extraServo3, angle, extraPos3); break; // 13
    case 4: moveServoToAngle(extraServo4, angle, extraPos4); break; // 17
    case 5: moveServoToAngle(extraServo5, angle, extraPos5); break; // 18
    case 6: moveServoToAngle(extraServo6, angle, extraPos6); break; // 19
    default: Serial.println("Bad Servo #"); break;
  }
}

// ---------------- Serial receive + parse ----------------

void recvWithStartEndMarkers() {
  static boolean recvInProgress = false;
  static byte ndx = 0;
  char startMarker = '<';
  char endMarker = '>';
  char rc;

  while (Serial.available() > 0 && newData == false) {
    rc = Serial.read();

    if (recvInProgress == true) {
      if (rc != endMarker) {
        receivedChars[ndx] = rc;
        ndx++;
        if (ndx >= numChars) ndx = numChars - 1;
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

// Safe parsing (supports 0..3 numbers after command)
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

// ---------------- Command processing ----------------

void showParsedData() {
  if (messageFromPC == 'M') {
    setMotorHorzDirection((int)floatFromPC1);
  } else if (messageFromPC == 'N') {
    setMotorVertDirection((int)floatFromPC1);
  } else if (messageFromPC == 'L') {
    setDistanceHorz((int)floatFromPC1);
  } else if (messageFromPC == 'P') {
    setDistanceVert((int)floatFromPC1);
  } else if (messageFromPC == 'B') {
    setDistanceBoth((int)floatFromPC1, (int)floatFromPC2);
  } else if (messageFromPC == 'H') {
    Homing();
  } else if (messageFromPC == 'S') {
    servo((int)floatFromPC1);   // KEEP OLD SERVO CONTROL (0/1)
  } else if (messageFromPC == 'X') {
    SyringeMove((int)floatFromPC1, (int)floatFromPC2, (int)floatFromPC3);
  } else if (messageFromPC == 'A') {
    // NEW: <A,servoNumber,angle>
    setExtraServoAngle((int)floatFromPC1, (int)floatFromPC2);
  } else {
    Serial.println("?");
  }
}

// ---------------- Setup + loop ----------------

void setup() {
  pinMode(stepPin1, OUTPUT);
  pinMode(dirPin1, OUTPUT);

  pinMode(stepPin2, OUTPUT);
  pinMode(dirPin2, OUTPUT);

  pinMode(stepPin3, OUTPUT);
  pinMode(dirPin3, OUTPUT);

  pinMode(syrdirpin, OUTPUT);
  pinMode(syrsteppin, OUTPUT);

  // Attach servos
  myservo.attach(servopin);              // existing servo (pin 14) unchanged

  extraServo1.attach(extraServoPin1);    // pin 11
  extraServo2.attach(extraServoPin2);    // pin 12
  extraServo3.attach(extraServoPin3);    // pin 13
  extraServo4.attach(extraServoPin4);    // pin 17
  extraServo5.attach(extraServoPin5);    // pin 18
  extraServo6.attach(extraServoPin6);    // pin 19

  // Initialize extra servos to their tracked positions.
  extraServo1.write(extraPos1);
  extraServo2.write(extraPos2);
  extraServo3.write(extraPos3);
  extraServo4.write(extraPos4);
  extraServo5.write(extraPos5);
  extraServo6.write(extraPos6);

  Serial.begin(115200);
  while (!Serial) { delay(1); }
  Serial.println("We are Connected");
  Serial.println("Init: valves 1-3 set to 40 (closed to dobot)");
}

void loop() {
  recvWithStartEndMarkers();
  if (newData == true) {
    parseData();
    showParsedData();
    newData = false;
  }
}
