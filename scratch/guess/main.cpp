#include <iostream>

int main() {
  const int secret = 7;
  std::cout << "Guess a number (1-10): ";
  int guess = 0;
  if (!(std::cin >> guess)) {
    std::cout << "Invalid input\n";
    return 1;
  }
  if (guess < 1 || guess > 10) {
    std::cout << "Out of range\n";
    return 1;
  }
  if (guess == secret) {
    std::cout << "Correct!\n";
  } else {
    std::cout << "Nope, the number was " << secret << ".\n";
  }
  return 0;
}
